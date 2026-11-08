"""Spans, metrics and log lines the worker emits (in-memory OpenTelemetry SDK, no collector)."""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from evilflowers_lcpencrypt_worker import _is_prefork, lcpencrypt
from evilflowers_lcpencrypt_worker.helpers import ExecutableException, ExecutableResult, run_executable
from evilflowers_lcpencrypt_worker.log_format import JsonFormatter, TextFormatter, configure_logging, log_format

_SPANS = InMemorySpanExporter()
_METRICS = InMemoryMetricReader()


@pytest.fixture(scope="session", autouse=True)
def sdk_providers():
    """Install real SDK providers once: the global providers can be set only once per process."""
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(_SPANS))
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(MeterProvider(metric_readers=[_METRICS]))
    yield


@pytest.fixture
def spans():
    _SPANS.clear()
    return _SPANS


def _points(name: str) -> list:
    data = _METRICS.get_metrics_data()
    found = []
    for resource_metrics in data.resource_metrics if data else []:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == name:
                    found.extend(metric.data.data_points)
    return found


def _count(name: str, **attributes: str) -> float:
    return sum(
        point.value if hasattr(point, "value") else point.count
        for point in _points(name)
        if all(point.attributes.get(key) == value for key, value in attributes.items())
    )


class TestTaskSpan:
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_success_span_and_metrics(self, mock_run, spans, monkeypatch, tmp_path):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        (tmp_path / "enc").mkdir()
        (tmp_path / "enc" / "cid-1.epub").write_bytes(b"encrypted")
        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")
        before = _count("lcpencrypt.encryptions", **{"lcp.outcome": "success", "lcp.input.format": "epub"})

        result = lcpencrypt.run(
            input_file="src/book.epub", contentid="cid-1", storage="enc", url="https://cdn", filename="cid-1"
        )

        assert result["success"] is True
        (span,) = spans.get_finished_spans()
        assert span.name == "lcpencrypt"
        assert span.attributes["lcp.content_id"] == "cid-1"
        assert span.attributes["lcp.input.format"] == "epub"
        assert span.attributes["lcp.storage_mode"] == 2
        assert span.attributes["lcp.outcome"] == "success"
        assert span.attributes["process.exit.code"] == 0
        assert span.attributes["lcp.protected_content.size"] == len(b"encrypted")
        assert span.status.status_code is StatusCode.UNSET
        after = _count("lcpencrypt.encryptions", **{"lcp.outcome": "success", "lcp.input.format": "epub"})
        assert after == before + 1
        assert _count("lcpencrypt.encryption.duration", **{"lcp.outcome": "success"}) >= 1

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_failure_marks_span_error(self, mock_run, spans, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")
        mock_run.side_effect = ExecutableException("lcpencrypt", 3, "", "boom")
        before = _count("lcpencrypt.encryptions", **{"lcp.outcome": "failure"})

        result = lcpencrypt.run(input_file="book.pdf", contentid="cid-2")

        assert result["success"] is False
        (span,) = spans.get_finished_spans()
        assert span.status.status_code is StatusCode.ERROR
        assert span.attributes["process.exit.code"] == 3
        assert span.attributes["lcp.outcome"] == "failure"
        assert [event.name for event in span.events] == ["exception"]
        assert _count("lcpencrypt.encryptions", **{"lcp.outcome": "failure"}) == before + 1

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_metadata_injection_flag(self, mock_run, spans, monkeypatch, tmp_path):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        (tmp_path / "book.pdf").write_bytes(b"%PDF-1.4")
        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        lcpencrypt.run(input_file="book.pdf", contentid="cid-3", title="T", temp=str(tmp_path / "tmp"))

        (span,) = spans.get_finished_spans()
        assert span.attributes["lcp.metadata.injected"] is True
        # A fake PDF: packaged with the metadata, without cover or page count.
        assert span.attributes["lcp.package.cover"] == "none"
        assert span.attributes["lcp.package.pages"] == 0
        assert "lcp.package.warning" in [event.name for event in span.events]

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_metadata_injection_failure_is_an_event(self, mock_run, spans, monkeypatch, tmp_path):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        lcpencrypt.run(input_file="missing.pdf", contentid="cid-4", title="T", temp=str(tmp_path / "tmp"))

        (span,) = spans.get_finished_spans()
        assert span.attributes["lcp.metadata.injected"] is False
        assert "lcp.metadata.injection_failed" in [event.name for event in span.events]


class TestReportTracing:
    """Issue #3: the report joins the task's trace; issue #4: the manifest is on the span."""

    @patch("evilflowers_lcpencrypt_worker.report.urllib.request.urlopen")
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_report_is_a_client_span_carrying_traceparent(self, mock_run, mock_urlopen, spans, monkeypatch, tmp_path):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")
        response = MagicMock(status=200)
        response.__enter__.return_value = response
        mock_urlopen.return_value = response

        lcpencrypt.run(input_file="book.epub", contentid="cid-r", report="http://u:p@catalog:8000/hook")

        by_name = {span.name: span for span in spans.get_finished_spans()}
        task_span, post = by_name["lcpencrypt"], by_name["POST"]
        assert post.kind is trace.SpanKind.CLIENT
        assert post.parent.span_id == task_span.context.span_id
        assert post.attributes["url.full"] == "http://catalog:8000/hook"  # no credentials
        assert post.attributes["http.response.status_code"] == 200
        request = mock_urlopen.call_args.args[0]
        traceparent = request.get_header("Traceparent")
        trace_hex, span_hex = traceparent.split("-")[1:3]
        assert trace_hex == trace.format_trace_id(task_span.context.trace_id)
        assert span_hex == trace.format_span_id(post.context.span_id)
        body = json.loads(request.data)
        assert body["trace_id"] == trace_hex

    @patch("evilflowers_lcpencrypt_worker.report.time.sleep")
    @patch("evilflowers_lcpencrypt_worker.report.urllib.request.urlopen")
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_failed_report_marks_only_its_own_span(self, mock_run, mock_urlopen, _sleep, spans, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")
        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")
        mock_urlopen.side_effect = OSError("refused")

        result = lcpencrypt.run(input_file="book.epub", contentid="cid-f", report="http://catalog/hook")

        assert result["success"] is True
        by_name = {span.name: span for span in spans.get_finished_spans()}
        assert by_name["POST"].status.status_code is StatusCode.ERROR
        assert by_name["POST"].attributes["http.request.resend_count"] == 2
        assert by_name["lcpencrypt"].status.status_code is StatusCode.UNSET

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_manifest_metadata_event(self, mock_run, spans, monkeypatch, tmp_path):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        (tmp_path / "book.pdf").write_bytes(b"%PDF-1.4")
        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        lcpencrypt.run(input_file="book.pdf", contentid="cid-m", title="Titul", authors=["A"], temp=str(tmp_path))

        (span,) = spans.get_finished_spans()
        (event,) = [event for event in span.events if event.name == "lcp.package.manifest"]
        metadata = json.loads(event.attributes["metadata"])
        assert metadata["title"] == "Titul"
        assert metadata["author"] == "A"


class TestFailureReason:
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_reason_from_stdout_is_in_error(self, mock_run, spans, monkeypatch):
        """lcpencrypt prints parameter errors on stdout (seen with v1.13.4 and v1.14.0)."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")
        mock_run.side_effect = ExecutableException(
            "lcpencrypt", 1, "Parameters : incorrect parameters, storage requires url \n", ""
        )

        result = lcpencrypt.run(input_file="book.epub", contentid="c", storage="enc", url="https://cdn")

        assert result["error"].startswith("Encryption failed: lcpencrypt exited with return code 1: ")
        assert result["error"].endswith("storage requires url")

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_storage_without_url_is_refused_up_front(self, mock_run, spans, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        result = lcpencrypt.run(input_file="book.epub", contentid="c", storage="enc")

        mock_run.assert_not_called()
        assert result["success"] is False
        assert result["error"] == "Invalid parameters: storage requires url"
        (span,) = spans.get_finished_spans()
        assert span.status.status_code is StatusCode.ERROR
        assert span.attributes["lcp.outcome"] == "failure"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_stdout_preferred_and_empty_streams_tolerated(self, mock_run, spans, monkeypatch):
        """Real v1.14 output for a missing input: the reason on stdout, Go `log` progress on stderr."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")
        mock_run.side_effect = ExecutableException("lcpencrypt", 2, "", "")
        assert (
            lcpencrypt.run(input_file="b.epub")["error"] == "Encryption failed: lcpencrypt exited with return code 2"
        )

        mock_run.side_effect = ExecutableException(
            "lcpencrypt",
            1,
            "Error processing a publication : open /data/src/missing.epub: no such file or directory\n",
            "2026/11/08 15:08:29 Process as EPUB\n",
        )
        assert lcpencrypt.run(input_file="b.epub")["error"].endswith(
            ": Error processing a publication : open /data/src/missing.epub: no such file or directory"
        )

        # Nothing on stdout: stderr is all there is.
        mock_run.side_effect = ExecutableException("lcpencrypt", 2, "", "first\nlast err\n")
        assert lcpencrypt.run(input_file="b.epub")["error"].endswith(": last err")


class TestJsonLogs:
    def _record(self, msg: str = "hello", **extra: object) -> logging.LogRecord:
        record = logging.LogRecord("evilflowers_lcpencrypt_worker", logging.INFO, __file__, 1, msg, (), None)
        record.__dict__.update(extra)
        return record

    def test_shape(self):
        line = json.loads(JsonFormatter().format(self._record(content_id="cid")))
        assert line["level"] == "INFO"
        assert line["logger"] == "evilflowers_lcpencrypt_worker"
        assert line["msg"] == "hello"
        assert line["time"].endswith("Z")
        assert line["content_id"] == "cid"
        assert "trace_id" not in line

    def test_trace_id_inside_span(self):
        with trace.get_tracer("test").start_as_current_span("s") as span:
            line = json.loads(JsonFormatter().format(self._record()))
            expected = trace.format_trace_id(span.get_span_context().trace_id)
        assert line["trace_id"] == expected
        assert len(line["span_id"]) == 16

    def test_extra_never_overwrites_alloy_fields(self):
        line = json.loads(JsonFormatter().format(self._record(level="DEBUG")))
        assert line["level"] == "INFO"

    def test_exception_is_serialized(self):
        try:
            raise ValueError("bad")
        except ValueError:
            import sys

            record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
        line = json.loads(JsonFormatter().format(record))
        assert "ValueError: bad" in line["exc"]

    def test_text_formatter_appends_trace_id(self):
        with trace.get_tracer("test").start_as_current_span("s"):
            line = TextFormatter().format(self._record())
        assert "trace_id=" in line

    @pytest.mark.parametrize(
        ("value", "expected"), [(None, "text"), ("json", "json"), ("JSON", "json"), ("x", "text")]
    )
    def test_log_format_env(self, monkeypatch, value, expected):
        if value is None:
            monkeypatch.delenv("LOG_FORMAT", raising=False)
        else:
            monkeypatch.setenv("LOG_FORMAT", value)
        assert log_format() == expected

    # Celery's setup_logging signal passes an int; LOG_LEVEL / the CLI pass a name.
    @pytest.mark.parametrize(("level", "expected"), [("warning", logging.WARNING), (logging.DEBUG, logging.DEBUG)])
    def test_configure_logging_installs_json_handler(self, monkeypatch, level, expected):
        monkeypatch.setenv("LOG_FORMAT", "json")
        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        try:
            configure_logging(level)
            assert root.level == expected
            assert isinstance(root.handlers[0].formatter, JsonFormatter)
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

    def test_celery_setup_logging_signal(self, monkeypatch):
        """The receiver Celery calls at worker start; it passes loglevel=logging.INFO."""
        from celery.signals import setup_logging

        monkeypatch.setenv("LOG_FORMAT", "json")
        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        try:
            setup_logging.send(sender=None, loglevel=logging.INFO, logfile=None, format="", colorize=False)
            assert root.level == logging.INFO
            assert isinstance(root.handlers[0].formatter, JsonFormatter)
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)


class TestLogRedaction:
    """Celery's own task lines carry the kwargs (lcpsv/notify with htpasswd credentials)."""

    KWARGS = "{'lcpsv': 'http://lcpuser:Hunter2pw@readium.svc:8989', 'notify': 'http://u:p@cms/hook'}"

    def _celery_like_record(self) -> logging.LogRecord:
        record = logging.LogRecord(
            "celery.worker.strategy",
            logging.INFO,
            __file__,
            1,
            "Task %s received: %s",
            ("evilflowers_lcpencrypt_worker.lcpencrypt[id]", self.KWARGS),
            None,
        )
        record.data = {"id": "id", "kwargs": self.KWARGS}
        return record

    @pytest.mark.parametrize("formatter", [JsonFormatter(), TextFormatter()], ids=["json", "text"])
    def test_credentials_never_reach_the_line(self, formatter):
        line = formatter.format(self._celery_like_record())
        assert "Hunter2pw" not in line
        assert "lcpuser" not in line
        assert "u:p@" not in line
        assert "***:***@readium.svc:8989" in line

    def test_json_stays_valid_and_keeps_the_extra(self):
        line = json.loads(JsonFormatter().format(self._celery_like_record()))
        assert "http://***:***@readium.svc:8989" in line["data"]["kwargs"]
        assert "http://***:***@readium.svc:8989" in line["msg"]

    @pytest.mark.parametrize(
        "text",
        ["https://example.com/a@b", "mailto:someone@example.com", "redis://lcp-redis:6379/7", "s3:eu-west-3:bucket"],
    )
    def test_urls_without_credentials_are_untouched(self, text):
        from evilflowers_lcpencrypt_worker.log_format import redact

        assert redact(text) == text


class TestCommandLogging:
    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_credentials_are_redacted(self, mock_run, caplog):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with caplog.at_level(logging.INFO, logger="evilflowers_lcpencrypt_worker.helpers"):
            run_executable(
                "lcpencrypt",
                kwargs_dict={"lcpsv": "http://admin:s3cret@readium:8989", "notify": "http://u:p@cms/hook"},
                kwargs_key_prefix="-",
            )
        text = caplog.text
        assert "s3cret" not in text
        assert "u:p@" not in text
        assert "http://***:***@readium:8989" in text

    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_stderr_on_success_is_not_a_warning(self, mock_run, caplog):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="Process as PDF")
        with caplog.at_level(logging.DEBUG, logger="evilflowers_lcpencrypt_worker.helpers"):
            run_executable("lcpencrypt")
        assert [r.levelno for r in caplog.records if "Process as PDF" in r.getMessage()] == [logging.DEBUG]

    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_stderr_on_failure_is_attached_to_error(self, mock_run, caplog):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no such file")
        with caplog.at_level(logging.ERROR), pytest.raises(ExecutableException):
            run_executable("lcpencrypt")
        (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert record.stderr == "no such file"


class TestPoolDetection:
    def test_prefork(self):
        from celery.concurrency.prefork import TaskPool

        assert _is_prefork(MagicMock(pool_cls=TaskPool)) is True

    def test_solo(self):
        from celery.concurrency.solo import TaskPool

        assert _is_prefork(MagicMock(pool_cls=TaskPool)) is False
