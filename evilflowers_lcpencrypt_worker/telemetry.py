"""OpenTelemetry for the worker (traces + metrics over OTLP/HTTP to the node's Grafana Alloy).

Same contract as the EvilFlowersCatalog (IP-019, `evil_flowers_catalog/telemetry.py`) and the
ELVIRA "Deploy a service" cookbook:

- Configuration is the standard `OTEL_*` environment. The pod sets `OTEL_SERVICE_NAME`,
  `OTEL_RESOURCE_ATTRIBUTES` and `OTEL_EXPORTER_OTLP_ENDPOINT=http://$(HOST_IP):4318`.
- Without an explicit endpoint, `HOST_IP` (Kubernetes downward API) and then the container's
  default gateway (Docker bridge → host) are probed for a collector on :4318. Nothing answering
  means telemetry stays off: no exporter retrying against localhost forever.
- Logs are not exported over OTLP. Alloy tails stdout (JSON lines, `log_format.py`); exporting
  them too would put every line into Loki twice.

Process model (Celery prefork): `discover_endpoint()` runs once in the main process
(`worker_init`) so the children inherit the verdict through the environment; `setup()` runs in
each child after the fork (`worker_process_init`); `shutdown()` flushes on
`worker_process_shutdown`, because children leave through `os._exit()` and skip `atexit`.

The task context arrives in the Celery message headers (the catalog's CeleryInstrumentor puts
it there), so a worker span continues the catalog's trace: upload → queue → lcpencrypt.

The SDK is the `observability` extra and is imported lazily; without it every call here is a
no-op and the worker behaves as if this module did not exist.
"""

import atexit
import importlib.metadata
import logging
import os
import socket
import struct
import threading
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Final

from opentelemetry import metrics

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Histogram
    from opentelemetry.sdk.metrics.export import MetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanProcessor

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_NAME: Final = "lcpencrypt"
INSTRUMENTATION_SCOPE: Final = "evilflowers_lcpencrypt_worker"
OTLP_HTTP_PORT: Final = 4318
PROBE_TIMEOUT_SECONDS: Final = 0.3
ROUTE_FILE: Final = Path("/proc/net/route")

ENDPOINT_VARIABLES: Final = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
)

# Set once a process tree has looked for the collector, so forked children inherit the verdict.
_DISCOVERY_MARKER: Final = "EVILFLOWERS_OTEL_DISCOVERY_DONE"

# `setdefault` only: an explicit operator value always wins.
ENVIRONMENT_DEFAULTS: Final = {
    "OTEL_SERVICE_NAME": DEFAULT_SERVICE_NAME,
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_SEMCONV_STABILITY_OPT_IN": "http",
    "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS": "process,os,host",
}

# Per-process metrics only: container CPU/memory come from cAdvisor, profiles from Pyroscope.
SYSTEM_METRICS_CONFIG: Final[dict[str, list[str] | None]] = {
    "process.cpu.time": ["user", "system"],
    "process.memory.usage": None,
    "process.thread.count": None,
    "process.open_file_descriptor.count": None,
}

_lock = threading.Lock()
_state: dict[str, bool] = {"configured": False, "enabled": False}
_providers: list[object] = []


# -- environment ----------------------------------------------------------------


def _env_flag(name: str, *values: str) -> bool:
    return os.environ.get(name, "").strip().lower() in values


def sdk_disabled() -> bool:
    return _env_flag("OTEL_SDK_DISABLED", "true")


def configured_endpoint() -> str | None:
    for name in ENDPOINT_VARIABLES:
        if value := os.environ.get(name, "").strip():
            return value
    return None


def exporter_names(variable: str) -> list[str]:
    """`OTEL_TRACES_EXPORTER` / `OTEL_METRICS_EXPORTER`, default `otlp`; `none` removes all."""
    names = [name.strip().lower() for name in os.environ.get(variable, "otlp").split(",") if name.strip()]
    return [name for name in names if name != "none"]


# -- collector discovery ----------------------------------------------------------


def parse_default_gateway(route_table: str) -> str | None:
    """IPv4 default gateway from the text of `/proc/net/route` (little-endian hex columns)."""
    for line in route_table.splitlines()[1:]:
        match line.split():
            case [_, "00000000", gateway, flags, *_]:
                try:
                    if not int(flags, 16) & 0x2:  # RTF_GATEWAY
                        continue
                    address = socket.inet_ntoa(struct.pack("<L", int(gateway, 16)))
                except ValueError, struct.error, OSError:
                    continue
                if address != "0.0.0.0":  # noqa: S104 - a parsed value, not a bind address
                    return address
            case _:
                continue
    return None


def default_gateway(route_file: Path = ROUTE_FILE) -> str | None:
    try:
        return parse_default_gateway(route_file.read_text())
    except OSError:
        return None


def probe(host: str, port: int = OTLP_HTTP_PORT, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """One TCP connect, nothing sent. True when something listens there."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _endpoint_for(host: str) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{OTLP_HTTP_PORT}"


def discover_endpoint() -> str | None:
    """Find the OTLP endpoint and export it into `os.environ` when discovered.

    Precedence: explicit `OTEL_EXPORTER_OTLP_*ENDPOINT`, then `HOST_IP`, then the default
    gateway. Probes at most once per process tree; `OTEL_EXPORTER_OTLP_AUTODISCOVER=false`
    turns the probing off.
    """
    if explicit := configured_endpoint():
        return explicit
    if os.environ.get(_DISCOVERY_MARKER) or _env_flag("OTEL_EXPORTER_OTLP_AUTODISCOVER", "false", "0", "no", "off"):
        return None

    os.environ[_DISCOVERY_MARKER] = "1"

    host = os.environ.get("HOST_IP", "").strip() or default_gateway()
    if not host:
        logger.info("OpenTelemetry: no OTLP endpoint configured and no default gateway found; telemetry is off")
        return None

    endpoint = _endpoint_for(host)
    if not probe(host):
        logger.info("OpenTelemetry: no collector answered at %s; telemetry is off", endpoint)
        return None

    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
    os.environ.setdefault("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    logger.info("OpenTelemetry: discovered collector at %s", endpoint)
    return endpoint


# -- setup ------------------------------------------------------------------------


def is_enabled() -> bool:
    return _state["enabled"]


def setup() -> bool:
    """Configure tracing and metrics for this process. Idempotent; returns whether it is on."""
    with _lock:
        if _state["configured"]:
            return _state["enabled"]
        _state["configured"] = True

        if sdk_disabled():
            return False

        endpoint = discover_endpoint()
        span_exporters = exporter_names("OTEL_TRACES_EXPORTER")
        metric_exporters = exporter_names("OTEL_METRICS_EXPORTER")
        if endpoint is None:
            span_exporters = [name for name in span_exporters if name != "otlp"]
            metric_exporters = [name for name in metric_exporters if name != "otlp"]
        if not span_exporters and not metric_exporters:
            return False

        try:
            _configure(span_exporters, metric_exporters)
        except ImportError as error:
            logger.warning("OpenTelemetry SDK is not installed (%s); telemetry is off", error)
            return False

        _state["enabled"] = True
        return True


def _configure(span_exporters: list[str], metric_exporters: list[str]) -> None:
    for name, value in ENVIRONMENT_DEFAULTS.items():
        os.environ.setdefault(name, value)

    from opentelemetry import trace
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
    from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace import TracerProvider

    resource = build_resource()

    tracer_provider = TracerProvider(resource=resource)
    for processor in _span_processors(span_exporters):
        tracer_provider.add_span_processor(processor)
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(resource=resource, metric_readers=_metric_readers(metric_exporters))
    metrics.set_meter_provider(meter_provider)

    _providers[:] = [tracer_provider, meter_provider]
    atexit.register(shutdown)

    CeleryInstrumentor().instrument()  # type: ignore[no-untyped-call]
    SystemMetricsInstrumentor(config=SYSTEM_METRICS_CONFIG).instrument()

    logger.info(
        "OpenTelemetry on: traces=%s metrics=%s endpoint=%s",
        ",".join(span_exporters) or "none",
        ",".join(metric_exporters) or "none",
        configured_endpoint() or "-",
    )


def service_version() -> str:
    try:
        return importlib.metadata.version("evilflowers-lcpencrypt-worker")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def build_resource() -> Resource:
    """Detectors, then our defaults, then the environment (`OTEL_RESOURCE_ATTRIBUTES` wins).

    `service.instance.id` is `<hostname>-<pid>`: the prefork children export the same metric
    series and Mimir needs them apart. Safe because `setup()` runs after the last fork.
    """
    from opentelemetry.sdk.resources import OTELResourceDetector, Resource

    for name, value in ENVIRONMENT_DEFAULTS.items():
        os.environ.setdefault(name, value)

    from_environment = OTELResourceDetector().detect().attributes
    defaults: dict[str, str] = {
        "service.name": DEFAULT_SERVICE_NAME,
        "service.namespace": "elvira",
        "service.version": service_version(),
        "service.instance.id": f"{socket.gethostname()}-{os.getpid()}",
        "lcpencrypt.version": os.environ.get("READIUM_LCPENCRYPT_VERSION", "unknown"),
    }
    return Resource.create({key: value for key, value in defaults.items() if key not in from_environment})


def _span_processors(names: list[str]) -> list[SpanProcessor]:
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    processors: list[SpanProcessor] = []
    for name in names:
        match name:
            case "otlp":
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                processors.append(BatchSpanProcessor(OTLPSpanExporter()))
            case "console":
                processors.append(BatchSpanProcessor(ConsoleSpanExporter()))
            case _:
                logger.warning("OpenTelemetry: unsupported traces exporter %r ignored", name)
    return processors


def _metric_readers(names: list[str]) -> list[MetricReader]:
    from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader

    readers: list[MetricReader] = []
    for name in names:
        match name:
            case "otlp":
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

                readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
            case "console":
                readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter()))
            case _:
                logger.warning("OpenTelemetry: unsupported metrics exporter %r ignored", name)
    return readers


def shutdown() -> None:
    """Flush and stop the providers. Idempotent; safe from any exit path."""
    with _lock:
        providers, _providers[:] = list(_providers), []
    for provider in providers:
        try:
            provider.shutdown()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - never let exit fail on telemetry
            logger.debug("OpenTelemetry provider shutdown failed", exc_info=True)


# -- instruments --------------------------------------------------------------------
# Created through the API: before `setup()` (or without the SDK) they are no-op proxies that
# start recording once a real MeterProvider is set.


@cache
def encryption_duration() -> Histogram:
    return metrics.get_meter(INSTRUMENTATION_SCOPE, service_version()).create_histogram(
        "lcpencrypt.encryption.duration",
        unit="s",
        description="Wall time of one lcpencrypt run, by outcome and input format.",
    )


@cache
def encryptions() -> Counter:
    return metrics.get_meter(INSTRUMENTATION_SCOPE, service_version()).create_counter(
        "lcpencrypt.encryptions",
        unit="{encryption}",
        description="lcpencrypt runs, by outcome and input format.",
    )


@cache
def protected_content_size() -> Histogram:
    return metrics.get_meter(INSTRUMENTATION_SCOPE, service_version()).create_histogram(
        "lcpencrypt.protected_content.size",
        unit="By",
        description="Size of the encrypted publication written to local storage.",
    )


__all__ = [
    "build_resource",
    "configured_endpoint",
    "default_gateway",
    "discover_endpoint",
    "encryption_duration",
    "encryptions",
    "is_enabled",
    "parse_default_gateway",
    "probe",
    "protected_content_size",
    "setup",
    "shutdown",
]
