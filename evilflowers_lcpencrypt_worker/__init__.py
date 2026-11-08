"""EvilFlowers LCP Encryption Worker.

A Celery worker that encrypts publications with Readium LCP (Licensed Content Protection) by
wrapping the `lcpencrypt` command-line tool. EvilFlowersCatalog queues the task; lcpencrypt
registers the content with the License Server and calls the catalog's notify webhook.
"""

import hashlib
import json
import logging
import mimetypes
import os
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Final, Unpack

from celery import Celery, Task
from celery.signals import setup_logging, worker_init, worker_process_init, worker_process_shutdown
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from evilflowers_lcpencrypt_worker import telemetry
from evilflowers_lcpencrypt_worker.helpers import CliValue, ExecutableException, run_executable
from evilflowers_lcpencrypt_worker.log_format import configure_logging
from evilflowers_lcpencrypt_worker.types import LCPEncryptParams, LCPEncryptResult, StorageMode

logger = logging.getLogger(__name__)

QUEUE: Final = "evilflowers_lcpencrypt_worker"

app = Celery("evilflowers_lcpencrypt_worker", broker=os.getenv("BROKER", "redis://localhost:6379/7"))
app.conf.update(
    broker_connection_retry_on_startup=True,
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # One long-running subprocess per task: never reserve a second message behind it.
    worker_prefetch_multiplier=1,
)


# -- logging and telemetry ----------------------------------------------------------


@setup_logging.connect
def _configure_logging(loglevel: str | int | None = None, **_: Any) -> None:
    """Celery configures no logging of its own when this signal has a receiver.

    The worker, its pool children and the task code then log through one handler: JSON lines
    with `level` and `trace_id` (`LOG_FORMAT=json`, the container default) or plain text.
    """
    configure_logging(loglevel)


def _is_prefork(worker: object) -> bool:
    pool = getattr(worker, "pool_cls", None)
    return "prefork" in f"{getattr(pool, '__module__', '')}.{getattr(pool, '__name__', pool)}"


@worker_init.connect
def _discover_collector(sender: object = None, **_: Any) -> None:
    """Main process: look for the collector once, before the pool forks.

    Children inherit the verdict through the environment. With a non-forking pool (solo,
    threads) the tasks run in this process, so it is the one to set up.
    """
    telemetry.discover_endpoint()
    if sender is not None and not _is_prefork(sender):
        telemetry.setup()


@worker_process_init.connect
def _setup_telemetry_in_child(**_: Any) -> None:
    telemetry.setup()


@worker_process_shutdown.connect
def _flush_telemetry_in_child(**_: Any) -> None:
    # Prefork children leave through `os._exit()`, which skips `atexit`.
    telemetry.shutdown()


# -- helpers ------------------------------------------------------------------------

# Media types of LCP outputs and sources that `mimetypes` does not know.
_MIME_BY_SUFFIX: Final = {
    ".epub": "application/epub+zip",
    ".lcpdf": "application/pdf+lcp",
    ".lcpa": "application/audiobook+lcp",
    ".lcpdi": "application/divina+lcp",
    ".webpub": "application/webpub+lcp",
    ".pdf": "application/pdf",
    ".audiobook": "application/audiobook+zip",
}

# lcpencrypt renames its output to an LCP-specific extension (.pdf → .lcpdf, audiobook → .lcpa, …),
# so these are probed after the bare file name when looking for the encrypted file.
_OUTPUT_EXTENSIONS: Final = (".epub", ".lcpdf", ".lcpa", ".lcpdi", ".webpub", ".pdf", ".lpf", ".audiobook")

# Readium Web Publication Manifest constants used when pre-packaging a raw PDF.
RWPM_CONTEXT: Final = "https://readium.org/webpub-manifest/context.jsonld"
RWPM_PDF_PROFILE: Final = "https://readium.org/webpub-manifest/profiles/pdf"


def _determine_storage_mode(storage: str | None) -> StorageMode:
    """0 (not stored), 1 (S3) or 2 (file system), from the `storage` parameter."""
    match storage:
        case None | "":
            return 0
        case str() if storage.startswith("s3:"):
            return 1
        case _:
            return 2


def _get_file_info(file_path: str) -> tuple[str, int, str]:
    """`(mime_type, size, sha256)` of a file."""
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type is None:
        mime_type = _MIME_BY_SUFFIX.get(Path(file_path).suffix.lower(), "application/octet-stream")

    with open(file_path, "rb") as handle:
        digest = hashlib.file_digest(handle, "sha256")

    return mime_type, os.path.getsize(file_path), digest.hexdigest()


def _input_format(input_path: str) -> str:
    """Lower-case extension without the dot (`pdf`, `epub`, …) for span and metric attributes."""
    path = urllib.parse.urlsplit(input_path).path if "://" in input_path else input_path
    return Path(path).suffix.lower().lstrip(".") or "unknown"


def _looks_like_pdf(input_path: str) -> bool:
    """True if the input locator points to a PDF (by extension); query strings are ignored."""
    return _input_format(input_path) == "pdf"


def _localize_pdf(resolved_input: str, temp_dir: str) -> tuple[str, bool]:
    """Make the PDF available on the local file system.

    Returns `(local_path, downloaded)`; `downloaded` means a temporary copy the caller removes.
    """
    if resolved_input.startswith(("http://", "https://")):
        os.makedirs(temp_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(suffix=".pdf", dir=temp_dir)
        os.close(fd)
        urllib.request.urlretrieve(resolved_input, tmp_path)  # noqa: S310 - trusted catalog input
        return tmp_path, True
    return resolved_input, False


def _build_pdf_readium_package(pdf_path: str, package_path: str, title: str | None, author: str | None) -> None:
    """Wrap a raw PDF into a minimal Readium Package (RWPP) carrying bibliographic metadata.

    lcpencrypt reads a PDF's title/author only from the PDF's own metadata. Catalog PDFs usually
    have none, so the generated manifest falls back to a file-name title and no authors, and
    Thorium shows "no title and no authors available". lcpencrypt keeps an existing manifest for
    `.rpf` input, so a package whose `manifest.json` carries the metadata yields a correct
    `.lcpdf`.

    Args:
        pdf_path: Local path of the source PDF.
        package_path: Destination of the Readium package (should end in `.rpf`).
        title: Publication title; falls back to the PDF file stem.
        author: Publication author; omitted from the manifest when empty.
    """
    metadata: dict[str, Any] = {
        "@type": "http://schema.org/Book",
        "conformsTo": RWPM_PDF_PROFILE,
        # RWPM requires a title; mirror lcpencrypt's file-name fallback.
        "title": title or Path(pdf_path).stem,
    }
    if author:
        metadata["author"] = author

    manifest = {
        "@context": RWPM_CONTEXT,
        "metadata": metadata,
        "readingOrder": [{"href": "publication.pdf", "type": "application/pdf"}],
    }

    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(pdf_path, arcname="publication.pdf")
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))


def _resolve_local_output(local_file_path: str) -> str | None:
    """The encrypted file on disk: the exact path, or the path plus an LCP extension."""
    for candidate in (local_file_path, *(local_file_path + ext for ext in _OUTPUT_EXTENSIONS)):
        if Path(candidate).exists():
            return candidate
    return None


def _failure_reason(error: ExecutableException) -> str:
    """Last non-empty line lcpencrypt printed. It reports parameter errors on stdout, not stderr
    (e.g. `Parameters : incorrect parameters, storage requires url`)."""
    for stream in (error.stderr, error.stdout):
        lines = [line.strip() for line in (stream or "").splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return ""


def _content_key(stdout: str) -> str:
    """The content encryption key, when lcpencrypt printed it."""
    for line in stdout.splitlines():
        if "content encryption key" in line.lower():
            return line.split(":")[-1].strip()
    return ""


# -- task -----------------------------------------------------------------------------


@app.task(
    bind=True,
    name="evilflowers_lcpencrypt_worker.lcpencrypt",
    queue=QUEUE,
    acks_late=True,
    reject_on_worker_lost=True,
)
# `Task[...]` is generic only in the celery-types stubs; Celery reads `__annotations__` when it
# builds the task, so the subscripted form must stay a string at runtime.
def lcpencrypt(
    self: "Task[Any, LCPEncryptResult]",  # noqa: UP037 - must not be evaluated at runtime, see above
    **params: Unpack[LCPEncryptParams],
) -> LCPEncryptResult:
    """Encrypt a publication with Readium LCP.

    Supported formats: EPUB 2/3, PDF, Readium Packages (audiobooks, DIVINA, …), W3C Audiobooks
    packaged as LPF. Parameters are described in `LCPEncryptParams`.

    For raw-PDF input with `title`/`author`, the PDF is first wrapped into a Readium package
    whose manifest carries that metadata (best effort: on failure the raw PDF is encrypted).

    Environment:
        STORAGE_PATH: Base path for file storage (default `/mnt/data`).
        READIUM_LCPENCRYPT_BIN: Path to the lcpencrypt binary (default `lcpencrypt`).

    Returns:
        LCPEncryptResult; `success` is False (and `error` set) when lcpencrypt fails.

    Example:
        >>> lcpencrypt.delay(input_file="book.epub", storage="s3:eu-west-3:lcp", contentid="book-123")
    """
    input_file = params["input_file"]
    storage = params.get("storage")
    url = params.get("url")
    contentid = params.get("contentid")
    filename = params.get("filename")
    title = params.get("title")
    author = params.get("author")
    temp = params.get("temp", "/tmp")  # noqa: S108 - lcpencrypt's own default, overridable per task
    output = params.get("output")

    storage_path = os.getenv("STORAGE_PATH", "/mnt/data")
    lcpencrypt_bin = os.getenv("READIUM_LCPENCRYPT_BIN", "lcpencrypt")
    input_format = _input_format(input_file)
    log_extra = {"content_id": contentid, "task_id": self.request.id}

    logger.info("Starting LCP encryption for %s", input_file, extra=log_extra)

    resolved_input = (
        input_file if input_file.startswith(("http://", "https://")) else str(Path(storage_path) / input_file)
    )

    cmd_args: dict[str, CliValue] = {
        "input": resolved_input,
        "contentid": contentid,
        "temp": temp,
        "verbose": params.get("verbose", False),
    }

    if storage:
        # Recommended mode: lcpencrypt stores the package itself.
        cmd_args["storage"] = storage if storage.startswith("s3:") else str(Path(storage_path) / storage)
        if url:
            cmd_args["url"] = url
        if filename:
            cmd_args["filename"] = filename
    elif output:
        # Legacy mode: temporary output location.
        cmd_args["output"] = output if os.path.isabs(output) else str(Path(storage_path) / output)

    optional: dict[str, str | None] = {
        "lcpsv": params.get("lcpsv"),
        "notify": params.get("notify"),
        "login": params.get("login"),
        "password": params.get("password"),
    }
    cmd_args |= {key: value for key, value in optional.items() if value}

    tracer = trace.get_tracer(telemetry.INSTRUMENTATION_SCOPE, telemetry.service_version())
    with tracer.start_as_current_span(
        "lcpencrypt",
        attributes={
            "lcp.content_id": contentid or "",
            "lcp.input.format": input_format,
            "lcp.storage_mode": _determine_storage_mode(storage),
            "lcp.metadata.injected": False,
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        package_temp_dir = _prepackage_pdf(cmd_args, resolved_input, temp, title, author, span)
        started = time.perf_counter()
        try:
            result = run_executable(executable_path=lcpencrypt_bin, kwargs_dict=cmd_args, kwargs_key_prefix="-")
        except ExecutableException as error:
            _record(span, input_format, "failure", time.perf_counter() - started)
            span.set_attribute("process.exit.code", error.returncode)
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, f"lcpencrypt exited with {error.returncode}"))
            # `error`, not `exception`: a Python traceback adds nothing to lcpencrypt's own stderr.
            logger.error(
                "LCP encryption failed for %s: %s",
                input_file,
                error,
                extra={**log_extra, "returncode": error.returncode, "stderr": error.stderr, "stdout": error.stdout},
            )
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
                error=f"Encryption failed: {error}" + (f": {reason}" if (reason := _failure_reason(error)) else ""),
            )
        finally:
            # Remove the temporary Readium package (and any downloaded PDF next to it).
            if package_temp_dir is not None:
                shutil.rmtree(package_temp_dir, ignore_errors=True)

        _record(span, input_format, "success", time.perf_counter() - started)
        span.set_attribute("process.exit.code", result.returncode)
        logger.info("LCP encryption completed for %s", input_file, extra=log_extra)
        logger.debug("lcpencrypt output: %s", result.stdout)

        output_filename = filename or contentid or Path(input_file).stem
        match storage:
            case str() if storage.startswith("s3:"):
                output_location = f"{url}/{output_filename}" if url else output_filename
                local_file_path = None
            case str() if storage:
                local_file_path = str(Path(storage_path) / storage / output_filename)
                output_location = f"{url}/{output_filename}" if url else local_file_path
            case _:
                local_file_path = str(Path(output or temp) / output_filename)
                output_location = local_file_path

        mime_type, file_size, file_hash = "application/octet-stream", 0, ""
        if local_file_path is not None and (found := _resolve_local_output(local_file_path)) is not None:
            mime_type, file_size, file_hash = _get_file_info(found)
            telemetry.protected_content_size().record(file_size, {"lcp.input.format": input_format})
            span.set_attribute("lcp.protected_content.size", file_size)

        return LCPEncryptResult(
            content_id=contentid or "generated",
            content_encryption_key=_content_key(result.stdout),
            storage_mode=_determine_storage_mode(storage),
            protected_content_location=output_location,
            protected_content_disposition=Path(input_file).name,
            protected_content_type=mime_type,
            protected_content_length=file_size,
            protected_content_sha256=file_hash,
            success=True,
            error="",
        )


def _prepackage_pdf(
    cmd_args: dict[str, CliValue],
    resolved_input: str,
    temp: str,
    title: str | None,
    author: str | None,
    span: trace.Span,
) -> str | None:
    """Swap a raw PDF input for a Readium package carrying title/author; returns its temp dir.

    lcpencrypt has no title/author flags, so for raw-PDF input with metadata the PDF is wrapped
    in a `.rpf` whose manifest lcpencrypt keeps. Best effort: on any failure the raw PDF is
    encrypted without the metadata rather than failing the task.
    """
    if not (title or author) or not _looks_like_pdf(resolved_input):
        return None

    package_temp_dir: str | None = None
    try:
        os.makedirs(temp, exist_ok=True)
        local_pdf, downloaded = _localize_pdf(resolved_input, temp)
        package_temp_dir = tempfile.mkdtemp(prefix="lcp-rwpp-", dir=temp)
        package_path = str(Path(package_temp_dir) / f"{Path(local_pdf).stem}.rpf")
        _build_pdf_readium_package(local_pdf, package_path, title, author)
        cmd_args["input"] = package_path
        span.set_attribute("lcp.metadata.injected", True)
        logger.info("Pre-packaged PDF into a Readium package with title=%r author=%r", title, author)
        if downloaded:
            os.remove(local_pdf)
    except Exception as error:  # noqa: BLE001 - metadata injection is best effort
        logger.warning("Failed to pre-package PDF with metadata, encrypting the raw input: %s", error)
        span.add_event("lcp.metadata.injection_failed", {"exception.message": str(error)})
    return package_temp_dir


def _record(span: trace.Span, input_format: str, outcome: str, seconds: float) -> None:
    attributes = {"lcp.input.format": input_format, "lcp.outcome": outcome}
    telemetry.encryption_duration().record(seconds, attributes)
    telemetry.encryptions().add(1, attributes)
    span.set_attribute("lcp.outcome", outcome)


__all__ = ["LCPEncryptParams", "LCPEncryptResult", "app", "lcpencrypt"]
