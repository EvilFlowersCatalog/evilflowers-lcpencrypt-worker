"""Report every task outcome to the catalog (issue #3).

lcpencrypt calls the catalog's `notify` webhook only on success, and the catalog runs without a
Celery result backend, so a failed run used to leave its job `pending` forever. When the task
gets a `report` URL it POSTs an `EncryptionReport` there once, whatever happened:

    POST {report}   Authorization: Basic …   traceparent: 00-<trace>-<span>-01
    {"contentid": "…", "task_id": "…", "trace_id": "…", "success": false, "error": "…",
     "report": {"cover": "rendered", "pages": 237, "toc_entries": 136, "warnings": [], …}}

The request is a client span under the task's `lcpencrypt` span and carries its context, so the
catalog's handler lands in the same trace. Credentials come from the URL (`http://user:pass@…`,
like `-notify`) and are never logged. A failed report is a warning: it never changes the task's
own result.
"""

import base64
import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Final

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from evilflowers_lcpencrypt_worker import telemetry

if TYPE_CHECKING:
    from evilflowers_lcpencrypt_worker.package import PackageReport
    from evilflowers_lcpencrypt_worker.types import EncryptionReport, LCPEncryptResult, PackageSummary

logger = logging.getLogger(__name__)

ATTEMPTS: Final = 3
TIMEOUT_SECONDS: Final = 10.0
BACKOFF_SECONDS: Final = (1.0, 3.0)
# Client errors that a later attempt can fix; any other 4xx is final.
_RETRYABLE_CLIENT_ERRORS: Final = frozenset({408, 425, 429})


def split_credentials(url: str) -> tuple[str, str | None]:
    """`(url without userinfo, Basic Authorization header or None)`.

    Mirrors lcpencrypt: credentials count only when the URL carries a username and a password.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.username is None or parts.password is None:
        return url, None
    netloc = parts.netloc.rpartition("@")[2]
    credentials = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password)}"
    authorization = "Basic " + base64.b64encode(credentials.encode()).decode()
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc)), authorization


def encryption_report(
    result: LCPEncryptResult,
    *,
    contentid: str | None,
    task_id: str | None,
    trace_id: str | None,
    package: PackageReport | None,
    manifest_path: str | None,
) -> EncryptionReport:
    """The report body. The content encryption key is deliberately left out."""
    summary: PackageSummary = {}
    if package is not None:
        summary |= {
            "cover": package.cover,
            "pages": package.page_count,
            "toc_entries": package.toc_entries,
            "warnings": list(package.warnings),
        }
    if manifest_path:
        summary["manifest"] = manifest_path
    if result["success"]:
        summary |= {
            "protected_content_location": result["protected_content_location"],
            "protected_content_type": result["protected_content_type"],
            "protected_content_length": result["protected_content_length"],
            "protected_content_sha256": result["protected_content_sha256"],
        }
    return {
        "contentid": contentid or "",
        "task_id": task_id or "",
        "trace_id": trace_id or "",
        "success": result["success"],
        "error": result["error"],
        "report": summary,
    }


def send_report(url: str, payload: EncryptionReport) -> bool:
    """POST `payload` to `url`, retrying transient failures. Returns whether the catalog took it."""
    target, authorization = split_credentials(url)
    parts = urllib.parse.urlsplit(target)
    if parts.scheme not in ("http", "https"):
        logger.warning("Not reporting the task outcome: report URL %s is not http(s)", target)
        return False

    body = json.dumps(payload, ensure_ascii=False).encode()
    tracer = trace.get_tracer(telemetry.INSTRUMENTATION_SCOPE, telemetry.service_version())
    with tracer.start_as_current_span(
        "POST",
        kind=SpanKind.CLIENT,
        attributes={
            "http.request.method": "POST",
            "url.full": target,
            "server.address": parts.hostname or "",
            "lcp.report.success": payload["success"],
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        headers = {"Content-Type": "application/json"}
        if authorization is not None:
            headers["Authorization"] = authorization
        propagate.inject(headers)  # traceparent of this span: the catalog's handler becomes its child

        reason, error_type = "", ""
        for attempt in range(1, ATTEMPTS + 1):
            if attempt > 1:
                span.set_attribute("http.request.resend_count", attempt - 1)
            request = urllib.request.Request(target, data=body, headers=headers, method="POST")  # noqa: S310
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                    span.set_attribute("http.response.status_code", response.status)
                    logger.info("Reported the task outcome to %s (HTTP %s)", target, response.status)
                    return True
            except urllib.error.HTTPError as error:
                error.close()
                span.set_attribute("http.response.status_code", error.code)
                reason, error_type = f"HTTP {error.code}", str(error.code)
                if error.code < 500 and error.code not in _RETRYABLE_CLIENT_ERRORS:
                    break
            # URLError, timeouts, refused connections; a half-written response (catalog restarting)
            # raises http.client's BadStatusLine / IncompleteRead, which are not OSErrors.
            except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
                reason, error_type = str(getattr(error, "reason", error)) or repr(error), type(error).__name__
            if attempt < ATTEMPTS:
                time.sleep(BACKOFF_SECONDS[attempt - 1])

        span.set_attribute("error.type", error_type)
        span.set_status(Status(StatusCode.ERROR, reason))
        logger.warning("Could not report the task outcome to %s: %s", target, reason)
        return False


__all__ = ["encryption_report", "send_report", "split_credentials"]
