"""Log formatting for the worker.

`JsonFormatter` writes one JSON object per line, the shape Grafana Alloy parses from container
stdout/stderr (same as EvilFlowersCatalog `evil_flowers_catalog/log_format.py`, IP-019): its
`stage.json` reads `level` and `trace_id`, and Grafana links `trace_id` to the Tempo trace.

    {"time": "2026-11-08T10:15:02.123Z", "level": "INFO", "logger": "evilflowers_lcpencrypt_worker",
     "msg": "...", "trace_id": "4bf9...", "span_id": "00f0...", "content_id": "..."}

`LOG_FORMAT=text` gives human-readable lines for a terminal. Standard library plus the
OpenTelemetry API (a base dependency); no SDK needed.
"""

import json
import logging
import logging.config
import os
import re
from datetime import UTC, datetime
from typing import Final, Literal

from opentelemetry import trace

type LogFormat = Literal["json", "text"]

# `scheme://user:password@host`: the catalog passes the License Server and notify URLs with
# credentials as task kwargs, and Celery's own "received"/"succeeded" lines log the kwargs.
_CREDENTIALS_IN_URL: Final = re.compile(r"(://)[^/@\s:'\"]+:[^/@\s'\"]+@")


def redact(text: str) -> str:
    """Replace URL credentials with `***:***`."""
    return _CREDENTIALS_IN_URL.sub(r"\1***:***@", text)


# Attributes every LogRecord has. Anything else on a record came from `extra=` and becomes a key.
_RECORD_ATTRIBUTES: Final = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "taskName",
}


def current_trace_ids() -> tuple[str, str] | tuple[None, None]:
    """`(trace_id, span_id)` of the current span as lowercase hex, or `(None, None)`."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None, None
    return trace.format_trace_id(span_context.trace_id), trace.format_span_id(span_context.span_id)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")
        payload: dict[str, object] = {
            "time": timestamp.replace("+00:00", "Z"),
            "level": record.levelname.upper(),
            "logger": record.name,
            "msg": record.getMessage(),
        }

        trace_id, span_id = current_trace_ids()
        if trace_id:
            payload["trace_id"] = trace_id
            payload["span_id"] = span_id

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            # An `extra=` key never overwrites the fields Alloy depends on.
            if key in _RECORD_ATTRIBUTES or key.startswith("_") or key in payload:
                continue
            payload[key] = value

        # On the serialized line, so `msg` and every `extra` (Celery's `data.kwargs`) are covered.
        return redact(json.dumps(payload, ensure_ascii=False, default=str))


class TextFormatter(logging.Formatter):
    """Human-readable lines for a terminal."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-5s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        line = redact(super().format(record))
        trace_id, _ = current_trace_ids()
        return f"{line} trace_id={trace_id}" if trace_id else line


def log_format() -> LogFormat:
    """`LOG_FORMAT` (`json` | `text`), default `text`. The container image sets `json`."""
    return "json" if os.getenv("LOG_FORMAT", "text").strip().lower() == "json" else "text"


def configure_logging(level: str | int | None = None) -> None:
    """Install one stdout handler on the root logger, replacing whatever was there.

    Celery calls this through its `setup_logging` signal, so it never installs its own plain-text
    handlers; the worker, its pool children and the task code all log through this formatter.
    """
    # Celery hands the `setup_logging` signal an int (logging.INFO); the env holds a name.
    raw: str | int = level or os.getenv("LOG_LEVEL") or "INFO"
    resolved = raw if isinstance(raw, int) else raw.strip().upper()
    formatter = f"{__name__}.JsonFormatter" if log_format() == "json" else f"{__name__}.TextFormatter"
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"default": {"()": formatter}},
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"level": resolved, "handlers": ["stdout"]},
        }
    )
    logging.captureWarnings(True)


__all__ = [
    "JsonFormatter",
    "LogFormat",
    "TextFormatter",
    "configure_logging",
    "current_trace_ids",
    "log_format",
    "redact",
]
