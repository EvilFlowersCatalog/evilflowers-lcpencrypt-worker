"""The kit's runtime in this worker (issue #8): settings, app, delivery cap, SIGTERM."""

import dataclasses
import json
import os
import signal
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from evilflowers_worker import runtime, testing
from pydantic import ValidationError

from evilflowers_lcpencrypt_worker import DeliveriesExhausted, app, lcpencrypt, package_preview
from evilflowers_lcpencrypt_worker.settings import QUEUE, Settings

ROOT = Path(__file__).resolve().parents[1]
# A db of its own: the tests delete their queue and read the shared `unacked` hash.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
# The pod's `terminationGracePeriodSeconds` (k8s default; role `evilflowers_k8s` sets none for lcpencrypt).
POD_TERMINATION_GRACE = 30


class TestSettings:
    def test_broker_from_the_roles_variable(self, monkeypatch):
        monkeypatch.setenv("BROKER", "redis://redis.shared:6379/7")
        assert Settings().redis_url == "redis://redis.shared:6379/7"

    def test_kits_variable_is_accepted(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "redis://other:6379/7")
        assert Settings().redis_url == "redis://other:6379/7"

    def test_defaults(self):
        current = Settings()
        assert current.redis_url == "redis://localhost:6379/7"
        assert current.queue == QUEUE == "evilflowers_lcpencrypt_worker"
        assert current.readium_lcpencrypt_bin == "lcpencrypt"
        assert current.manifest_sidecar is True
        assert current.max_deliveries == 5

    @pytest.mark.parametrize("value", ["false", "0", "no", "off"])
    def test_manifest_sidecar_off(self, monkeypatch, value):
        monkeypatch.setenv("MANIFEST_SIDECAR", value)
        assert Settings().manifest_sidecar is False

    def test_shutdown_fits_the_pods_grace(self):
        """Grace + cancel must end before the kubelet's SIGKILL, or the fallback requeue never runs."""
        current = Settings()
        assert current.worker_soft_shutdown_timeout + current.worker_cancel_timeout < POD_TERMINATION_GRACE


class TestApp:
    def test_task_names_are_the_catalogs_contract(self):
        assert "evilflowers_lcpencrypt_worker.lcpencrypt" in app.tasks
        assert "evilflowers_lcpencrypt_worker.package_preview" in app.tasks

    def test_routes_and_queue(self):
        assert app.conf.task_routes == {"evilflowers_lcpencrypt_worker.*": {"queue": QUEUE}}
        assert [queue.name for queue in app.conf.task_queues] == [QUEUE]

    def test_delivery_semantics(self):
        task = app.tasks["evilflowers_lcpencrypt_worker.lcpencrypt"]
        assert task.acks_late is True
        assert task.reject_on_worker_lost is True
        assert app.conf.worker_prefetch_multiplier == 1
        # The kit's shutdown replaces Celery's soft shutdown.
        assert app.conf.worker_soft_shutdown_timeout == 0
        assert app.conf.worker_send_task_events is True


class TestDeliveryCap:
    """A publication that kills the worker is redelivered; past MAX_DELIVERIES it is given up and reported."""

    @patch("evilflowers_lcpencrypt_worker.send_report")
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_past_the_cap_reports_failure_and_raises(self, mock_run, mock_send, monkeypatch):
        monkeypatch.setattr(runtime.runtime().deliveries, "record", lambda *_a, **_k: 6)

        with pytest.raises(DeliveriesExhausted, match="after 5 deliveries"):
            lcpencrypt.run(input_file="book.pdf", contentid="cid-x", report="http://u:p@catalog/hook")

        mock_run.assert_not_called()
        (url, payload), _ = mock_send.call_args
        assert url == "http://u:p@catalog/hook"
        assert payload["success"] is False
        assert payload["contentid"] == "cid-x"
        assert "Gave up after 5 deliveries" in payload["error"]

    @patch("evilflowers_lcpencrypt_worker.send_report")
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_at_the_cap_still_runs(self, mock_run, mock_send, monkeypatch):
        from evilflowers_lcpencrypt_worker.helpers import ExecutableResult

        monkeypatch.setattr(runtime.runtime().deliveries, "record", lambda *_a, **_k: 5)
        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        assert lcpencrypt.run(input_file="book.epub", contentid="cid-y")["success"] is True
        mock_run.assert_called_once()

    @patch("evilflowers_lcpencrypt_worker.send_report")
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_cap_off(self, mock_run, mock_send, monkeypatch):
        from evilflowers_lcpencrypt_worker.helpers import ExecutableResult

        current = runtime.runtime()
        monkeypatch.setattr(runtime, "_runtime", dataclasses.replace(current, settings=Settings(max_deliveries=0)))
        monkeypatch.setattr(current.deliveries, "record", lambda *_a, **_k: 99)
        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        assert lcpencrypt.run(input_file="book.epub")["success"] is True


class TestMessage:
    @patch("evilflowers_lcpencrypt_worker.send_report")
    def test_without_input_file_is_reported_and_fails_at_once(self, mock_send):
        with pytest.raises(ValidationError):
            lcpencrypt.run(contentid="c", report="http://catalog/hook", lcpsv="http://u:Hunter2pw@lcp")
        (url, payload), _ = mock_send.call_args
        assert url == "http://catalog/hook"
        assert payload["success"] is False
        assert payload["contentid"] == "c"
        assert payload["error"] == "Invalid task message: input_file: Field required"
        assert "Hunter2pw" not in json.dumps(payload)

    @patch("evilflowers_lcpencrypt_worker.send_report")
    def test_invalid_without_report_url_only_raises(self, mock_send):
        with pytest.raises(ValidationError):
            lcpencrypt.run(input_file=["not", "a", "path"])
        mock_send.assert_not_called()

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_unknown_kwargs_and_none_are_tolerated(self, mock_run):
        from evilflowers_lcpencrypt_worker.helpers import ExecutableResult

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")
        result = lcpencrypt.run(input_file="book.epub", contentid=None, storage=None, a_newer_catalog_field=1)
        assert result["success"] is True
        assert "storage" not in mock_run.call_args.kwargs["kwargs_dict"]

    def test_preview_is_not_capped(self):
        assert package_preview.run(input_file="book.epub")["success"] is False  # EPUB: refused, not raised


def _redis():
    client = testing.redis_client(TEST_REDIS_URL)
    if client is None:
        if os.environ.get("CI"):
            pytest.fail(f"no Redis at {TEST_REDIS_URL}")
        pytest.skip(f"no Redis at {TEST_REDIS_URL}")
    return client


@pytest.mark.skipif(sys.platform == "darwin", reason="prefork + spawn on macOS fails tasks before the body runs")
class TestRealWorker:
    """A real prefork worker against Redis, as in the pod (`testing.WorkerProcess`)."""

    def test_sigterm_mid_encryption_requeues_at_once(self, tmp_path):
        _redis()
        sleeper = tmp_path / "lcpencrypt"
        sleeper.write_text("#!/bin/sh\nexec sleep 120\n")
        sleeper.chmod(sleeper.stat().st_mode | stat.S_IEXEC)

        with (
            testing.StubCatalogServer() as catalog,
            testing.WorkerProcess(
                "evilflowers_lcpencrypt_worker",
                redis_url=TEST_REDIS_URL,
                pythonpath=[str(ROOT)],
                env={
                    "READIUM_LCPENCRYPT_BIN": str(sleeper),
                    "STORAGE_PATH": str(tmp_path),
                    "WORKER_SOFT_SHUTDOWN_TIMEOUT": "1",
                    "WORKER_CANCEL_TIMEOUT": "5",
                    "WORKER_HEARTBEAT_FILE": str(tmp_path / "heartbeat"),
                },
            ) as worker,
        ):
            worker.wait_ready()
            task_id = worker.send(
                "evilflowers_lcpencrypt_worker.lcpencrypt",
                input_file="book.epub",
                contentid="cid-sigterm",
                report=f"{catalog.url}/hook",
            )
            assert testing.wait_for(lambda: task_id in worker.unacked_ids(), 30), "the task never started"

            worker.signal(signal.SIGTERM)
            worker.wait(timeout=20)

            queued = worker.queued()
            assert [message["headers"]["id"] for message in queued] == [task_id], worker.output
            assert worker.kwargs(queued[0])["contentid"] == "cid-sigterm"
            assert task_id not in worker.unacked_ids()
            # A deliberate requeue is no outcome: nothing reported to the catalog.
            assert catalog.calls == []

    @pytest.mark.parametrize("log_format", ["text", "json"])
    def test_credentials_never_reach_the_worker_log(self, tmp_path, log_format):
        """The kwargs carry htpasswd credentials. An invalid message (the kit logs pydantic's error, which echoes
        the input) and a failed run (lcpencrypt's stdout goes into the error line verbatim; this fake prints its
        argv) both pass the credentials to the log; the redacting formatter must mask them in the real worker."""
        _redis()
        echo = tmp_path / "lcpencrypt"
        echo.write_text('#!/bin/sh\necho "bad run: $*"\nexit 1\n')
        echo.chmod(echo.stat().st_mode | stat.S_IEXEC)
        credentials = {"lcpsv": "http://lcpuser:Hunter2pw@readium:8989", "notify": "http://cms:S3cretNotify@cat/hook"}

        with (
            testing.StubCatalogServer() as catalog,
            testing.WorkerProcess(
                "evilflowers_lcpencrypt_worker",
                redis_url=TEST_REDIS_URL,
                pythonpath=[str(ROOT)],
                env={
                    "LOG_FORMAT": log_format,
                    "READIUM_LCPENCRYPT_BIN": str(echo),
                    "STORAGE_PATH": str(tmp_path),
                    "WORKER_HEARTBEAT_FILE": str(tmp_path / "heartbeat"),
                },
            ) as worker,
        ):
            worker.wait_ready()
            task, report = "evilflowers_lcpencrypt_worker.lcpencrypt", f"{catalog.url}/hook"
            worker.send(task, contentid="cid-invalid", report=report, **credentials)
            worker.send(task, input_file="book.epub", contentid="cid-failed", report=report, **credentials)
            assert testing.wait_for(lambda: len(catalog.calls) == 2, 30), "both outcomes should be reported"
            testing.wait_for(lambda: False, 2)  # let the failure lines reach stdout

        assert "Invalid message" in worker.output
        assert "bad run:" in worker.output
        assert "Hunter2pw" not in worker.output
        assert "S3cretNotify" not in worker.output
        assert "http://***:***@readium:8989" in worker.output  # the echoed argv, masked by the formatter
        errors = sorted(body["error"] for _method, _path, body in catalog.calls)
        assert errors[1] == "Invalid task message: input_file: Field required"
        assert errors[0].startswith("Encryption failed: ")
        assert "exited with return code 1" in errors[0]
        # The reported error is masked too: the catalog stores it.
        assert "Hunter2pw" not in errors[0]
        assert "http://***:***@readium:8989" in errors[0]

    def test_a_publication_that_kills_the_worker_is_given_up(self, tmp_path):
        """Each run kills its pool child (as an OOM would); past MAX_DELIVERIES the task is reported failed."""
        _redis()
        killer = tmp_path / "lcpencrypt"
        killer.write_text("#!/bin/sh\nkill -9 $PPID\nsleep 5\n")
        killer.chmod(killer.stat().st_mode | stat.S_IEXEC)

        with (
            testing.StubCatalogServer() as catalog,
            testing.WorkerProcess(
                "evilflowers_lcpencrypt_worker",
                redis_url=TEST_REDIS_URL,
                pythonpath=[str(ROOT)],
                env={
                    "READIUM_LCPENCRYPT_BIN": str(killer),
                    "STORAGE_PATH": str(tmp_path),
                    "MAX_DELIVERIES": "2",
                    "WORKER_HEARTBEAT_FILE": str(tmp_path / "heartbeat"),
                },
            ) as worker,
        ):
            worker.wait_ready()
            task_id = worker.send(
                "evilflowers_lcpencrypt_worker.lcpencrypt",
                input_file="book.epub",
                contentid="cid-oom",
                report=f"{catalog.url}/hook",
            )
            assert testing.wait_for(lambda: catalog.calls, 90), worker.output

            ((method, path, body),) = catalog.calls
            assert (method, path) == ("POST", "/hook")
            assert body["task_id"] == task_id
            assert body["success"] is False
            assert body["error"].startswith("Gave up after 2 deliveries")
            assert testing.wait_for(lambda: task_id not in worker.unacked_ids() and not worker.queued(), 10)
