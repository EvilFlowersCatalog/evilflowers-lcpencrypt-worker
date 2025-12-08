"""EvilFlowers LCP Encryption Worker.

A Celery-based worker for encrypting publications using the Readium LCP (Licensed Content Protection)
standard. This worker wraps the lcpencrypt command-line tool and provides a robust async task interface.
"""

import hashlib
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any, Unpack

from celery import Celery, Task

from evilflowers_lcpencrypt_worker.helpers import ExecutableException, run_executable
from evilflowers_lcpencrypt_worker.types import (
    LCPEncryptParams,
    LCPEncryptResult,
    StorageMode,
)

# Initialize logger
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Initialize Celery app
app = Celery(
    "evilflowers_lcpencrypt_worker",
    broker=os.getenv("BROKER", "redis://localhost:6379/0"),
)
app.conf.broker_connection_retry_on_startup = True
app.conf.task_track_started = True
app.conf.task_serializer = "json"
app.conf.result_serializer = "json"
app.conf.accept_content = ["json"]

# Optional: Set up OpenTelemetry tracing if available
try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    service_name = os.getenv("OTEL_SERVICE_NAME", "evilflowers-lcpencrypt-worker")
    resource = Resource(attributes={"service.name": service_name})
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(OTLPSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    CeleryInstrumentor().instrument()
    logger.info("OpenTelemetry tracing initialized")

except ImportError:
    logger.warning("OpenTelemetry not installed, tracing disabled")


def _determine_storage_mode(storage: str | None) -> StorageMode:
    """Determine storage mode based on storage parameter.

    Args:
        storage: Storage location string.

    Returns:
        StorageMode: 0 (not stored), 1 (S3), or 2 (filesystem).
    """
    if not storage:
        return 0
    if storage.startswith("s3:"):
        return 1
    return 2


def _get_file_info(file_path: str) -> tuple[str, int, str]:
    """Get file metadata (mime type, size, and SHA-256 hash).

    Args:
        file_path: Path to the file.

    Returns:
        Tuple of (mime_type, file_size, sha256_hash).
    """
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type is None:
        # Default MIME types for common LCP formats
        if file_path.endswith(".epub"):
            mime_type = "application/epub+zip"
        elif file_path.endswith(".pdf"):
            mime_type = "application/pdf"
        elif file_path.endswith(".audiobook"):
            mime_type = "application/audiobook+zip"
        else:
            mime_type = "application/octet-stream"

    file_size = os.path.getsize(file_path)

    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256_hash.update(chunk)

    return mime_type, file_size, sha256_hash.hexdigest()


@app.task(
    bind=True,
    name="evilflowers_lcpencrypt_worker.lcpencrypt",
    queue="evilflowers_lcpencrypt_worker",
    acks_late=True,
    reject_on_worker_lost=True,
)
def lcpencrypt(self: Task, **params: Unpack[LCPEncryptParams]) -> LCPEncryptResult:
    """Encrypt a publication using Readium LCP encryption.

    This task wraps the lcpencrypt command-line tool to encrypt EPUB, PDF, and other
    supported publication formats using the LCP (Licensed Content Protection) standard.

    Supported formats:
        - EPUB 2 and EPUB 3 (including multimedia)
        - PDF
        - Readium Packages (Audiobooks, DIVINA, etc.)
        - W3C Audiobooks packaged as LPF

    Args:
        **params: LCP encryption parameters. See LCPEncryptParams for details.

    Required parameters:
        input_file: Path to source file (relative to STORAGE_PATH or absolute URL)

    Recommended mode parameters (for storing in custom location):
        storage: Target storage location (S3 bucket or filesystem path)
        url: Public base URL corresponding to storage location

    Optional parameters:
        contentid: Unique content identifier (UUID generated if omitted)
        filename: Output filename (uses contentid if omitted)
        temp: Temporary working directory (default: /tmp)
        lcpsv: License server endpoint (format: http://user:pass@host)
        notify: CMS notification endpoint (format: http://user:pass@host)
        verbose: Enable verbose logging

    Legacy mode parameters:
        output: Temporary output directory (for legacy mode)
        login: License server login (legacy mode)
        password: License server password (legacy mode)

    Returns:
        LCPEncryptResult containing:
            - content_id: Content identifier
            - content_encryption_key: Encryption key (extracted from output)
            - storage_mode: How content is stored (0=not stored, 1=S3, 2=filesystem)
            - protected_content_location: URL or path to encrypted file
            - protected_content_disposition: Original filename
            - protected_content_type: MIME type
            - protected_content_length: File size in bytes
            - protected_content_sha256: SHA-256 hash
            - success: True if operation succeeded

    Raises:
        ExecutableException: If the lcpencrypt binary fails.

    Environment variables:
        STORAGE_PATH: Base path for file storage (default: /mnt/data)
        READIUM_LCPENCRYPT_BIN: Path to lcpencrypt binary (default: lcpencrypt)

    Example:
        >>> from evilflowers_lcpencrypt_worker import lcpencrypt
        >>> result = lcpencrypt.delay(
        ...     input_file="book.epub",
        ...     storage="s3:eu-west-3:lcp-storage",
        ...     url="https://lcp-storage.s3.eu-west-3.amazonaws.com",
        ...     contentid="book-123",
        ...     verbose=True
        ... )
        >>> print(result.get())
    """
    # Extract parameters
    input_file = params["input_file"]
    storage = params.get("storage")
    url = params.get("url")
    contentid = params.get("contentid")
    filename = params.get("filename")
    temp = params.get("temp", "/tmp")
    lcpsv = params.get("lcpsv")
    notify = params.get("notify")
    verbose = params.get("verbose", False)

    # Legacy mode parameters
    output = params.get("output")
    login = params.get("login")
    password = params.get("password")

    storage_path = os.getenv("STORAGE_PATH", "/mnt/data")
    lcpencrypt_bin = os.getenv("READIUM_LCPENCRYPT_BIN", "lcpencrypt")

    logger.info(f"Starting LCP encryption for: {input_file}")
    logger.debug(f"Task ID: {self.request.id}, Storage: {storage}, URL: {url}")

    # Resolve input file path
    if input_file.startswith(("http://", "https://")):
        resolved_input = input_file
    else:
        resolved_input = str(Path(storage_path) / input_file)

    # Build command arguments
    cmd_args: dict[str, Any] = {
        "input": resolved_input,
        "contentid": contentid,
        "temp": temp,
        "verbose": verbose,
    }

    # Add mode-specific parameters
    if storage and url:
        # Recommended mode: custom storage location
        if storage.startswith("s3:"):
            cmd_args["storage"] = storage
        else:
            cmd_args["storage"] = str(Path(storage_path) / storage)
        cmd_args["url"] = url
        cmd_args["filename"] = filename
    elif output:
        # Legacy mode: temporary output location
        cmd_args["output"] = str(Path(storage_path) / output) if not output.startswith("/") else output

    # Add optional parameters
    if lcpsv:
        cmd_args["lcpsv"] = lcpsv
    if notify:
        cmd_args["notify"] = notify
    if login:
        cmd_args["login"] = login
    if password:
        cmd_args["password"] = password

    try:
        # Execute lcpencrypt
        result = run_executable(
            executable_path=lcpencrypt_bin,
            kwargs_dict=cmd_args,
            kwargs_key_prefix="-",
        )

        logger.info(f"LCP encryption completed successfully for: {input_file}")
        logger.debug(f"Command output: {result.stdout}")

        # Determine output file location
        output_filename = filename or contentid or Path(input_file).stem
        if storage:
            if storage.startswith("s3:"):
                output_location = f"{url}/{output_filename}"
            else:
                output_path = Path(storage_path) / storage / output_filename
                output_location = f"{url}/{output_filename}"
        else:
            output_path = Path(output or temp) / output_filename
            output_location = str(output_path)

        # Get file information (if file is local)
        mime_type = "application/octet-stream"
        file_size = 0
        file_hash = ""

        if not storage or not storage.startswith("s3:"):
            if Path(output_location).exists():
                mime_type, file_size, file_hash = _get_file_info(output_location)
            else:
                # Try to find the encrypted file
                possible_extensions = [".epub", ".pdf", ".lpf", ".audiobook"]
                for ext in possible_extensions:
                    test_path = Path(str(output_location) + ext)
                    if test_path.exists():
                        output_location = str(test_path)
                        mime_type, file_size, file_hash = _get_file_info(output_location)
                        break

        # Extract content encryption key from output (if available)
        content_key = ""
        for line in result.stdout.split("\n"):
            if "content encryption key" in line.lower():
                content_key = line.split(":")[-1].strip()
                break

        return LCPEncryptResult(
            content_id=contentid or "generated",
            content_encryption_key=content_key,
            storage_mode=_determine_storage_mode(storage),
            protected_content_location=output_location,
            protected_content_disposition=Path(input_file).name,
            protected_content_type=mime_type,
            protected_content_length=file_size,
            protected_content_sha256=file_hash,
            success=True,
            error="",
        )

    except ExecutableException as e:
        logger.error(f"LCP encryption failed for {input_file}: {e}")
        logger.error(f"Stderr: {e.stderr}")
        logger.error(f"Stdout: {e.stdout}")

        return LCPEncryptResult(
            content_id=contentid or "unknown",
            content_encryption_key="",
            storage_mode=_determine_storage_mode(storage),
            protected_content_location="",
            protected_content_disposition=Path(input_file).name,
            protected_content_type="",
            protected_content_length=0,
            protected_content_sha256="",
            success=False,
            error=f"Encryption failed: {e}",
        )


__all__ = ["app", "lcpencrypt", "LCPEncryptParams", "LCPEncryptResult"]
