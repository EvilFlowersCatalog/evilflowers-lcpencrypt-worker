"""The outcome report the worker POSTs to the catalog (issue #3)."""

import base64
import http.client
import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from evilflowers_lcpencrypt_worker import lcpencrypt
from evilflowers_lcpencrypt_worker.helpers import ExecutableResult
from evilflowers_lcpencrypt_worker.log_format import redact
from evilflowers_lcpencrypt_worker.package import PackageReport
from evilflowers_lcpencrypt_worker.report import encryption_report, send_report, split_credentials

URLOPEN = "evilflowers_lcpencrypt_worker.report.urllib.request.urlopen"
SLEEP = "evilflowers_lcpencrypt_worker.report.time.sleep"

SUCCESS = {
    "content_id": "cid",
    "content_encryption_key": "SECRET-KEY",
    "storage_mode": 2,
    "protected_content_location": "https://elvira.example/content/cid.lcpdf",
    "protected_content_disposition": "book.pdf",
    "protected_content_type": "application/pdf+lcp",
    "protected_content_length": 42,
    "protected_content_sha256": "ab" * 32,
    "success": True,
    "error": "",
}


def _response(status=200):
    response = MagicMock()
    response.status = status
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def _http_error(code):
    return urllib.error.HTTPError("http://catalog/hook", code, "error", {}, io.BytesIO(b""))


class TestSplitCredentials:
    def test_credentials_become_basic_auth(self):
        url, authorization = split_credentials("http://lcp:s%40cret@catalog:8000/readium/v1/hooks/report")

        assert url == "http://catalog:8000/readium/v1/hooks/report"
        assert authorization == "Basic " + base64.b64encode(b"lcp:s@cret").decode()

    def test_url_without_credentials(self):
        assert split_credentials("https://catalog/hook") == ("https://catalog/hook", None)

    def test_username_without_password_is_not_auth(self):
        """Like lcpencrypt: only a username *and* a password count."""
        assert split_credentials("http://lcp@catalog/hook") == ("http://lcp@catalog/hook", None)

    def test_report_url_is_redacted_in_logs(self):
        assert redact("'report': 'http://lcp:secret@catalog/hook'") == "'report': 'http://***:***@catalog/hook'"


class TestEncryptionReport:
    def test_success_with_package(self):
        package = PackageReport(cover="rendered", page_count=237, toc_entries=136, warnings=["w"])

        report = encryption_report(
            SUCCESS,
            contentid="cid",
            task_id="task-1",
            trace_id="ab" * 16,
            package=package,
            manifest_path="enc/cid.manifest.json",
        )

        assert report == {
            "contentid": "cid",
            "task_id": "task-1",
            "trace_id": "ab" * 16,
            "success": True,
            "error": "",
            "report": {
                "cover": "rendered",
                "pages": 237,
                "toc_entries": 136,
                "warnings": ["w"],
                "manifest": "enc/cid.manifest.json",
                "protected_content_location": "https://elvira.example/content/cid.lcpdf",
                "protected_content_type": "application/pdf+lcp",
                "protected_content_length": 42,
                "protected_content_sha256": "ab" * 32,
            },
        }

    def test_never_carries_the_content_key(self):
        report = encryption_report(
            SUCCESS, contentid="cid", task_id="t", trace_id=None, package=None, manifest_path=None
        )

        assert "SECRET-KEY" not in json.dumps(report)

    def test_failure_without_package(self):
        failed = {**SUCCESS, "success": False, "error": "Encryption failed: boom"}

        report = encryption_report(
            failed, contentid=None, task_id=None, trace_id=None, package=None, manifest_path=None
        )

        assert report == {
            "contentid": "",
            "task_id": "",
            "trace_id": "",
            "success": False,
            "error": "Encryption failed: boom",
            "report": {},
        }


PAYLOAD = encryption_report(
    {**SUCCESS, "success": False, "error": "boom"},
    contentid="cid",
    task_id="t",
    trace_id="",
    package=None,
    manifest_path=None,
)


class TestSendReport:
    def test_posts_json_with_basic_auth(self):
        with patch(URLOPEN, return_value=_response(204)) as urlopen:
            assert send_report("http://lcp:secret@catalog:8000/hooks/report", PAYLOAD) is True

        (request,), kwargs = urlopen.call_args
        assert request.full_url == "http://catalog:8000/hooks/report"
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Basic " + base64.b64encode(b"lcp:secret").decode()
        assert request.get_header("Content-type") == "application/json"
        assert json.loads(request.data) == PAYLOAD
        assert kwargs["timeout"] > 0

    def test_server_error_is_retried(self):
        with (
            patch(URLOPEN, side_effect=[_http_error(503), _response(200)]) as urlopen,
            patch(SLEEP) as sleep,
        ):
            assert send_report("http://catalog/hook", PAYLOAD) is True

        assert urlopen.call_count == 2
        sleep.assert_called_once()

    def test_network_error_gives_up_after_three_attempts(self, caplog):
        with (
            patch(URLOPEN, side_effect=urllib.error.URLError("connection refused")) as urlopen,
            patch(SLEEP) as sleep,
            caplog.at_level("WARNING"),
        ):
            assert send_report("http://lcp:secret@catalog/hook", PAYLOAD) is False

        assert urlopen.call_count == 3
        assert sleep.call_count == 2
        assert "connection refused" in caplog.text
        assert "secret" not in caplog.text

    @pytest.mark.parametrize("code", [400, 401, 404])
    def test_client_error_is_final(self, code):
        with patch(URLOPEN, side_effect=_http_error(code)) as urlopen, patch(SLEEP):
            assert send_report("http://catalog/hook", PAYLOAD) is False

        assert urlopen.call_count == 1

    def test_too_many_requests_is_retried(self):
        with patch(URLOPEN, side_effect=[_http_error(429), _response(200)]) as urlopen, patch(SLEEP):
            assert send_report("http://catalog/hook", PAYLOAD) is True

        assert urlopen.call_count == 2

    def test_non_http_url_is_refused(self):
        with patch(URLOPEN) as urlopen:
            assert send_report("file:///etc/passwd", PAYLOAD) is False

        urlopen.assert_not_called()

    def test_half_written_response_is_retried_not_raised(self):
        """A catalog pod restarting mid-response: http.client errors are not OSErrors."""
        broken = [http.client.BadStatusLine(""), http.client.IncompleteRead(b"")]
        with patch(URLOPEN, side_effect=[*broken, _response(200)]) as urlopen, patch(SLEEP):
            assert send_report("http://catalog/hook", PAYLOAD) is True

        assert urlopen.call_count == 3


class TestReportNeverChangesTheTask:
    """The task's own result, or the exception it re-raises, survives any reporting problem."""

    @patch(SLEEP)
    @patch(URLOPEN, side_effect=http.client.BadStatusLine(""))
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_success_survives_a_broken_catalog(self, mock_run, _urlopen, _sleep, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        result = lcpencrypt.run(input_file="book.epub", contentid="cid", report="http://catalog/hook")

        assert result["success"] is True

    @patch("evilflowers_lcpencrypt_worker.send_report", side_effect=RuntimeError("report bug"))
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_report_bug_is_swallowed(self, mock_run, _send, monkeypatch, caplog):
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")
        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        with caplog.at_level("WARNING"):
            result = lcpencrypt.run(input_file="book.epub", contentid="cid", report="http://catalog/hook")

        assert result["success"] is True
        assert "report bug" in caplog.text

    @patch("evilflowers_lcpencrypt_worker.send_report", side_effect=RuntimeError("report bug"))
    @patch("evilflowers_lcpencrypt_worker.run_executable", side_effect=ValueError("the real cause"))
    def test_worker_bug_keeps_its_own_exception(self, _run, _send, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        with pytest.raises(ValueError, match="the real cause"):
            lcpencrypt.run(input_file="book.epub", contentid="cid", report="http://catalog/hook")
