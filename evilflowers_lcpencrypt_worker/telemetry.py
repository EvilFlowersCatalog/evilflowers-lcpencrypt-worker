"""What lcpencrypt adds to the kit's telemetry: its own instruments and span attributes (worker #8).

Providers, collector endpoint, resource, Celery/requests instrumentation, JSON logs and Pyroscope are the kit's
(evilflowers-worker `observability`, started by `runtime.create_app`). The `lcp.*` span attributes and the
`lcpencrypt.*` metrics stay here; their names are what the Grafana dashboard `elvira-lcp-encryption` queries.

Endpoint: `OTEL_EXPORTER_OTLP_ENDPOINT`, else derived from `HOST_IP`; with neither, telemetry is off. (The old
default-gateway probe for Docker Compose is gone: set the endpoint explicitly there.)
"""

import os
from typing import Final

from evilflowers_worker.observability.metrics import Instrument

#: Scope of the hand-made spans (`lcpencrypt`, `lcpencrypt.package_preview`, the report's `POST`).
INSTRUMENTATION_SCOPE: Final = "evilflowers_lcpencrypt_worker"

#: Bytes: a small EPUB up to a large audiobook.
SIZE_BUCKETS: Final = (1e4, 1e5, 5e5, 1e6, 5e6, 1e7, 5e7, 1e8, 5e8, 1e9, 5e9)

ENCRYPTION_DURATION: Final = Instrument(
    "histogram",
    "lcpencrypt.encryption.duration",
    "s",
    "Wall time of one lcpencrypt run, by outcome and input format.",
)
ENCRYPTIONS: Final = Instrument(
    "counter",
    "lcpencrypt.encryptions",
    "{encryption}",
    "lcpencrypt runs, by outcome and input format.",
)
PROTECTED_CONTENT_SIZE: Final = Instrument(
    "histogram",
    "lcpencrypt.protected_content.size",
    "By",
    "Size of the encrypted publication written to local storage.",
    buckets=SIZE_BUCKETS,
)


def service_version() -> str:
    """The worker's package version (the kit's `service.version` is the image tag)."""
    import importlib.metadata

    try:
        return importlib.metadata.version("evilflowers-lcpencrypt-worker")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def lcpencrypt_version() -> str:
    """The pinned lcpencrypt release (`READIUM_LCPENCRYPT_VERSION`, set by the image); a span attribute."""
    return os.environ.get("READIUM_LCPENCRYPT_VERSION", "unknown")


__all__ = [
    "ENCRYPTIONS",
    "ENCRYPTION_DURATION",
    "INSTRUMENTATION_SCOPE",
    "PROTECTED_CONTENT_SIZE",
    "lcpencrypt_version",
    "service_version",
]
