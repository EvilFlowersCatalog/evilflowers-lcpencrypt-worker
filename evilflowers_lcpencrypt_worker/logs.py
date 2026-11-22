"""Log lines: the kit's format (evilflowers-worker `observability.logs`), with URL credentials redacted (worker #8).

The kit sets the format: JSON lines (`time`, `level`, `logger`, `message`, `trace_id`, `span_id`, `task_name`,
`task_id`, the fields bound with `observability.bind`, `exception`) with `LOG_FORMAT=json`, Celery's own text format
otherwise. This module adds one thing on top: the catalog passes the License Server, notify and report URLs with
htpasswd credentials as task kwargs, and Celery's own "received" lines log the kwargs. `RedactingFormatter` wraps
whatever formatter a handler has and masks `scheme://user:password@` on the finished line, so the message, the
exception text and every bound field are covered in both formats.

`install()` connects the receiver after `runtime.create_app`, so it runs after the kit's own JSON receiver and wraps
the formatter that one set.
"""

import logging
import re
from typing import Any, Final

from celery import signals
from opentelemetry import trace

# `scheme://user:password@host`.
_CREDENTIALS_IN_URL: Final = re.compile(r"(://)[^/@\s:'\"]+:[^/@\s'\"]+@")


def redact(text: str) -> str:
    """Replace URL credentials with `***:***`."""
    return _CREDENTIALS_IN_URL.sub(r"\1***:***@", text)


def current_trace_ids() -> tuple[str, str] | tuple[None, None]:
    """`(trace_id, span_id)` of the current span as lowercase hex, or `(None, None)`."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None, None
    return trace.format_trace_id(span_context.trace_id), trace.format_span_id(span_context.span_id)


class RedactingFormatter(logging.Formatter):
    """Formats with `inner`, then masks URL credentials on the finished line."""

    def __init__(self, inner: logging.Formatter | None = None) -> None:
        super().__init__()
        self.inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return redact(self.inner.format(record))


def redact_handlers(logger: logging.Logger | None = None, **_: Any) -> None:
    """Celery's `after_setup_logger` / `after_setup_task_logger` receiver: wrap every handler's formatter once."""
    for handler in (logger or logging.getLogger()).handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(handler.formatter))


_installed = False


def install() -> None:
    """Connect `redact_handlers`. Idempotent; call it after `runtime.create_app`."""
    global _installed
    if _installed:
        return
    _installed = True
    signals.after_setup_logger.connect(redact_handlers, weak=False)
    signals.after_setup_task_logger.connect(redact_handlers, weak=False)


__all__ = ["RedactingFormatter", "current_trace_ids", "install", "redact", "redact_handlers"]
