import os

import logging
from typing import Optional

from celery import Celery, Task

from evilflowers_lcpencrypt_worker.helpers import run_executable

# Initialize logger
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# Initialize Celery app with broker and include new settings
app = Celery("evilflowers_lcpencrypt_worker", broker=os.getenv("BROKER", "redis://localhost:6379/0"))

# Optional: Set up OpenTelemetry tracing if available
try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.instrumentation.celery import CeleryInstrumentor

    # Set up OpenTelemetry tracing
    service_name = os.getenv("OTEL_SERVICE_NAME", "evilflowers-lcpencrypt-worker")
    resource = Resource(attributes={"service.name": service_name})
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(OTLPSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    # Instrument Celery with OpenTelemetry
    CeleryInstrumentor().instrument()

    logger.info("OpenTelemetry tracing initialized.")

except ImportError:
    logger.warning("OpenTelemetry not installed. Tracing disabled.")


@app.task(bind=True)
def lcpencrypt(
    self: Task,
    input_file: str,
    contentid: Optional[str] = None,
    storage: Optional[str] = None,
    url: Optional[str] = None,
    filename: Optional[str] = None,
    temp: str = "/tmp",
    cover: bool = False,
    contentkey: Optional[str] = None,
    lcpsv: Optional[str] = None,
    v2: bool = False,
    username: Optional[str] = None,
    password: Optional[str] = None,
    notify: Optional[str] = None,
    verbose: bool = False,
):
    target = f"/tmp/{self.request.id}"
    logger.debug(f"Temporary target: {target}")

    storage_path = os.getenv("STORAGE_PATH", "/mnt/data")

    kwargs_dict = {
        "input": f"{storage_path}/{input_file}",
        "contentid": contentid,
        "storage": storage if storage.startswith("s3://") else f"{storage_path}/{storage}",
        "url": url,
        "filename": filename,
        "temp": temp,
        "cover": cover,
        "contentkey": contentkey,
        "lcpsv": lcpsv,
        "v2": v2,
        "username": username,
        "password": password,
        "notify": notify,
        "verbose": verbose,
    }

    run_executable(os.getenv("READIUM_LCPENCRYPT_BIN"), kwargs_dict=kwargs_dict, kwargs_key_prefix="-")
