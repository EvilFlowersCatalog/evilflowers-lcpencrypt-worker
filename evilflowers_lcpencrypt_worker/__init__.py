"""EvilFlowers LCP Encryption Worker.

A Celery worker that encrypts publications with Readium LCP (Licensed Content Protection) by
wrapping the `lcpencrypt` command-line tool. EvilFlowersCatalog queues the task; lcpencrypt
registers the content with the License Server and calls the catalog's notify webhook; the worker
reports every outcome to the catalog's `report` URL when it gets one.
"""

import base64
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn, Unpack, cast

from celery.signals import worker_init
from evilflowers_worker import observability, runtime
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from evilflowers_lcpencrypt_worker import logs, telemetry
from evilflowers_lcpencrypt_worker.helpers import CliValue, ExecutableException, run_executable
from evilflowers_lcpencrypt_worker.logs import current_trace_ids
from evilflowers_lcpencrypt_worker.package import (
    PackageReport,
    PublicationMetadata,
    build_pdf_package,
    localize,
    preview_pdf_package,
    suffix_of,
)
from evilflowers_lcpencrypt_worker.report import encryption_report, send_report
from evilflowers_lcpencrypt_worker.settings import QUEUE, settings
from evilflowers_lcpencrypt_worker.types import (
    LCPEncryptMessage,
    LCPEncryptParams,
    LCPEncryptResult,
    PackagePreviewResult,
    StorageMode,
)

if TYPE_CHECKING:
    from celery import Celery, Task
    from pydantic import ValidationError

logger = logging.getLogger(__name__)

# -- the app: the kit's runtime (worker #8) ------------------------------------------------
# `create_app` gives the db's delivery semantics: late ack + requeue on a lost worker, prefetch 1, the db-wide
# visibility timeout (`BROKER_VISIBILITY_TIMEOUT`, worker #6), the SIGTERM protocol (warm shutdown, cancel and
# requeue after the grace), the delivery cap (`MAX_DELIVERIES`), task events, the liveness heartbeat, telemetry and
# JSON logs. The task names are a contract with the catalog, hence the explicit route.

_settings = settings()

# Typed here: the kit ships no py.typed, and an `Any` app would make every task decorator untyped.
app: Celery = runtime.create_app(
    "lcpencrypt",
    _settings,
    include=[],
    queues=[_settings.queue],
    routes={"evilflowers_lcpencrypt_worker.*": _settings.queue},
    attributes={**observability.CATALOG_JOB_ATTRIBUTES, "content_id": "lcp.content_id"},
)
# After `create_app`: wraps the formatter the kit's receiver sets.
logs.install()


@worker_init.connect
def _setup_telemetry_without_pool(sender: object = None, **_: Any) -> None:
    """With a non-forking pool (solo, threads) `worker_process_init` never fires: set up in this process."""
    if sender is not None and not _is_prefork(sender):
        observability.setup()


def _is_prefork(worker: object) -> bool:
    pool = getattr(worker, "pool_cls", None)
    return "prefork" in f"{getattr(pool, '__module__', '')}.{getattr(pool, '__name__', pool)}"


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

# lcpencrypt v1.14 prints this on stdout when the CMS (`-notify`) refuses or misses the call. It
# then withdraws the content from the License Server again and still exits 0.
_CMS_NOTIFICATION_ERROR: Final = "Error notifying the CMS"

# Size cap of the manifest metadata recorded as a span event.
_MANIFEST_EVENT_MAX_CHARS: Final = 4096


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


def _resolve(locator: str, storage_path: str) -> str:
    """An http(s) URL as is, anything else relative to STORAGE_PATH."""
    return locator if locator.startswith(("http://", "https://")) else str(Path(storage_path) / locator)


def _resolve_local_output(local_file_path: str) -> str | None:
    """The encrypted file on disk: the exact path, or the path plus an LCP extension."""
    for candidate in (local_file_path, *(local_file_path + ext for ext in _OUTPUT_EXTENSIONS)):
        if Path(candidate).exists():
            return candidate
    return None


def _failure_reason(error: ExecutableException) -> str:
    """Last non-empty line lcpencrypt printed, stdout first.

    lcpencrypt's `exitWithError` prints the reason on stdout (`Parameters : incorrect
    parameters, storage requires url`, `Error processing a publication : …`); stderr carries Go
    `log` progress such as `Process as EPUB`, which is no reason.
    """
    for stream in (error.stdout, error.stderr):
        lines = [line.strip() for line in (stream or "").splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return ""


def _cms_notification_error(stdout: str) -> str:
    """lcpencrypt's `Error notifying the CMS: …` line, or empty."""
    for line in stdout.splitlines():
        if line.strip().startswith(_CMS_NOTIFICATION_ERROR):
            return line.strip()
    return ""


def _content_key(stdout: str) -> str:
    """The content encryption key, when lcpencrypt printed it."""
    for line in stdout.splitlines():
        if "content encryption key" in line.lower():
            return line.split(":")[-1].strip()
    return ""


def _sidecar_enabled() -> bool:
    """`MANIFEST_SIDECAR` (default on): write `{filename}.manifest.json` next to the package."""
    return settings().manifest_sidecar


def _local_storage_dir(storage: str | None, storage_path: str) -> Path | None:
    """The file-system storage directory, or None for S3 and legacy mode."""
    if not storage or storage.startswith("s3:"):
        return None
    return Path(storage_path) / storage


# -- tasks ----------------------------------------------------------------------------


@dataclass(slots=True)
class _Outcome:
    """What one run produced: the task result plus what the report needs."""

    result: LCPEncryptResult
    package: PackageReport | None = None
    manifest_path: str | None = None


@app.task(bind=True, name="evilflowers_lcpencrypt_worker.lcpencrypt", queue=QUEUE, **runtime.JOB_TASK_OPTIONS)
# `Task[...]` is generic only in the celery-types stubs; Celery reads `__annotations__` when it
# builds the task, so the subscripted form must stay a string at runtime.
def lcpencrypt(
    self: "Task[Any, LCPEncryptResult]",  # noqa: UP037 - must not be evaluated at runtime, see above
    **kwargs: Any,
) -> LCPEncryptResult:
    """Encrypt a publication with Readium LCP.

    The kwargs are `LCPEncryptParams`. The body runs through the kit's `runtime.run_task`: the message is validated
    (no `input_file`: the task fails at once and is not retried), the delivery counted, and past `MAX_DELIVERIES`
    (the publication killed the worker every time) `_give_up` reports a failure instead of running lcpencrypt again.

    Supported formats: EPUB 2/3, PDF, Readium Packages (audiobooks, DIVINA, …), W3C Audiobooks
    packaged as LPF. Parameters are described in `LCPEncryptParams`.

    For raw-PDF input with publication metadata (`title`, `authors`, `publisher`, …), the PDF is
    first wrapped into a Readium package whose manifest carries that metadata plus what
    lcpencrypt would have added itself: a cover, `numberOfPages` and the PDF's outline as `toc`
    (best effort: on failure the raw PDF is encrypted). The manifest is also written next to the
    package as `{filename}.manifest.json` (file-system storage, `MANIFEST_SIDECAR`).

    With a `report` URL the outcome is POSTed there once, failures included (see `report.py`).

    Environment:
        STORAGE_PATH: Base path for file storage (default `/mnt/data`).
        READIUM_LCPENCRYPT_BIN: Path to the lcpencrypt binary (default `lcpencrypt`).
        MANIFEST_SIDECAR: `false` turns the manifest sidecar off (default on).

    Returns:
        LCPEncryptResult; `success` is False (and `error` set) when lcpencrypt fails or withdraws
        the publication because the CMS notification failed.

    Example:
        >>> lcpencrypt.delay(input_file="book.epub", storage="s3:eu-west-3:lcp", contentid="book-123")
    """
    task_id = self.request.id

    def run(message: LCPEncryptMessage, delivery: int) -> LCPEncryptResult:
        return _run(message.params(), task_id, delivery)

    def give_up(message: LCPEncryptMessage, delivery: int) -> NoReturn:
        _give_up(message.params(), task_id, delivery)

    def invalid(raw: dict[str, Any], error: ValidationError) -> NoReturn:
        _invalid(raw, error, task_id)

    return cast(
        "LCPEncryptResult",
        runtime.run_task(self, run, LCPEncryptMessage, kwargs, on_capped=give_up, on_invalid=invalid),
    )


def _invalid(raw: dict[str, Any], error: ValidationError, task_id: str | None) -> NoReturn:
    """A message that does not validate: report `success: false` when it has a `report` URL, then raise.

    Every outcome reaches the catalog (#3), this one included; the raise fails the task at once, without a retry.
    The reason names the fields only, never their values (the kwargs carry credentials).
    """
    reason = "Invalid task message: " + "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'message'}: {item['msg']}" for item in error.errors()
    )
    if isinstance(url := raw.get("report"), str) and url:
        contentid = raw.get("contentid") if isinstance(raw.get("contentid"), str) else None
        result = LCPEncryptResult(
            content_id=contentid or "unknown",
            content_encryption_key="",
            storage_mode=0,
            protected_content_location="",
            protected_content_disposition="",
            protected_content_type="",
            protected_content_length=0,
            protected_content_sha256="",
            success=False,
            error=reason,
        )
        try:
            trace_id, _ = current_trace_ids()
            payload = encryption_report(
                result, contentid=contentid, task_id=task_id, trace_id=trace_id, package=None, manifest_path=None
            )
            send_report(url, payload)
        except Exception as report_error:  # noqa: BLE001 - a report must not replace the validation error
            logger.warning("Reporting the invalid message of %s failed: %r", task_id, report_error)
    raise error


def _task_span(params: LCPEncryptParams, delivery: int) -> Any:
    """The `lcpencrypt` span of one delivery."""
    tracer = trace.get_tracer(telemetry.INSTRUMENTATION_SCOPE, telemetry.service_version())
    return tracer.start_as_current_span(
        "lcpencrypt",
        attributes={
            "lcp.content_id": params.get("contentid") or "",
            "lcp.input.format": _input_format(params["input_file"]),
            "lcp.storage_mode": _determine_storage_mode(params.get("storage")),
            "lcp.metadata.injected": False,
            "lcpencrypt.version": telemetry.lcpencrypt_version(),
            "evilflowers.job.delivery": delivery,
        },
        record_exception=False,
        set_status_on_exception=False,
    )


def _run(params: LCPEncryptParams, task_id: str | None, delivery: int) -> LCPEncryptResult:
    """One delivery of the task: encrypt, then report the outcome."""
    input_file = params["input_file"]
    contentid = params.get("contentid")

    # `content_id` on every log line of the run (the kit's JSON lines carry bound fields, not `extra=`).
    with observability.bind(content_id=contentid), _task_span(params, delivery) as span:
        logger.info("Starting LCP encryption for %s (delivery %d)", input_file, delivery)
        try:
            outcome = _encrypt(params, span=span)
        except Exception as error:
            # A bug, not an lcpencrypt failure: still tell the catalog, then let Celery see it.
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, type(error).__name__))
            _report(params, task_id, _Outcome(_failed(params, f"Worker error: {error!r}")))
            raise
        _report(params, task_id, outcome)
        return outcome.result


class DeliveriesExhausted(RuntimeError):
    """The message was delivered past `MAX_DELIVERIES` times: the run killed the worker every time."""


def _give_up(params: LCPEncryptParams, task_id: str | None, delivery: int) -> NoReturn:
    """Past the delivery cap: report `success: false` through the `report` URL, then raise (worker #8).

    lcpencrypt is not run again. The raise fails the task, so the message is acknowledged and never redelivered.
    """
    input_file = params["input_file"]
    error = (
        f"Gave up after {delivery - 1} deliveries: the worker died or was stopped during each of them "
        "(the publication may crash or exhaust the worker)"
    )
    with observability.bind(content_id=params.get("contentid")), _task_span(params, delivery) as span:
        _record(span, _input_format(input_file), "gave_up", 0.0)
        span.set_status(Status(StatusCode.ERROR, "delivery cap"))
        logger.error("LCP encryption of %s given up: %s", input_file, error)
        _report(params, task_id, _Outcome(_failed(params, error)))
    raise DeliveriesExhausted(error)


def _encrypt(params: LCPEncryptParams, *, span: trace.Span) -> _Outcome:
    """Run lcpencrypt for one task; every return is an outcome the catalog can be told about."""
    input_file = params["input_file"]
    storage = params.get("storage")
    url = params.get("url")
    contentid = params.get("contentid")
    filename = params.get("filename")
    temp = params.get("temp", "/tmp")  # noqa: S108 - lcpencrypt's own default, overridable per task
    output = params.get("output")

    current = settings()
    storage_path = str(current.storage_path)
    lcpencrypt_bin = current.readium_lcpencrypt_bin
    input_format = _input_format(input_file)

    if storage and not url:
        # lcpencrypt (v1.13.4+) exits with "storage requires url"; say so without running it.
        _record(span, input_format, "failure", 0.0)
        span.set_status(Status(StatusCode.ERROR, "storage requires url"))
        logger.error("LCP encryption refused for %s: storage requires url", input_file)
        return _Outcome(_failed(params, "Invalid parameters: storage requires url"))

    resolved_input = _resolve(input_file, storage_path)
    cmd_args: dict[str, CliValue] = {
        "input": resolved_input,
        "contentid": contentid,
        "temp": temp,
        "verbose": params.get("verbose", False),
    }

    if storage:
        # Recommended mode: lcpencrypt stores the package itself.
        cmd_args["storage"] = storage if storage.startswith("s3:") else str(Path(storage_path) / storage)
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

    package_temp_dir, package = _prepackage_pdf(
        cmd_args,
        resolved_input,
        temp,
        PublicationMetadata.from_params(params),
        cover=_resolve(cover, storage_path) if (cover := params.get("cover")) else None,
        # lcpencrypt names the stored cover after this entry, as its own PDF path does.
        cover_name=filename or contentid or "cover",
        span=span,
    )
    started = time.perf_counter()
    try:
        result = run_executable(executable_path=lcpencrypt_bin, kwargs_dict=cmd_args, kwargs_key_prefix="-")
    except ExecutableException as error:
        _record(span, input_format, "failure", time.perf_counter() - started)
        span.set_attribute("process.exit.code", error.returncode)
        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, f"lcpencrypt exited with {error.returncode}"))
        # `error`, not `exception`: a Python traceback adds nothing to lcpencrypt's own stderr.
        # lcpencrypt's own output in the line: the kit's JSON lines carry bound fields only, not `extra=`.
        logger.error(
            "LCP encryption failed for %s: %s\nstdout: %s\nstderr: %s",
            input_file,
            error,
            (error.stdout or "").strip(),
            (error.stderr or "").strip(),
        )
        reason = _failure_reason(error)
        message = f"Encryption failed: {error}" + (f": {reason}" if reason else "")
        return _Outcome(_failed(params, message), package)
    finally:
        # Remove the temporary Readium package (and any downloaded PDF next to it).
        if package_temp_dir is not None:
            shutil.rmtree(package_temp_dir, ignore_errors=True)

    span.set_attribute("process.exit.code", result.returncode)
    if cms_error := _cms_notification_error(result.stdout):
        # Exit 0, yet lcpencrypt took the content back off the License Server: not lendable.
        _record(span, input_format, "failure", time.perf_counter() - started)
        span.set_status(Status(StatusCode.ERROR, "CMS notification failed"))
        logger.error("LCP encryption of %s withdrawn: %s\nstdout: %s", input_file, cms_error, result.stdout.strip())
        message = f"Encryption withdrawn from the License Server: {cms_error}"
        return _Outcome(_failed(params, message), package)

    _record(span, input_format, "success", time.perf_counter() - started)
    logger.info("LCP encryption completed for %s", input_file)
    logger.debug("lcpencrypt output: %s", result.stdout)

    output_filename = filename or contentid or Path(input_file).stem
    match storage:
        case str() if storage.startswith("s3:"):
            output_location = f"{url}/{output_filename}"
            local_file_path = None
        case str() if storage:
            local_file_path = str(Path(storage_path) / storage / output_filename)
            output_location = f"{url}/{output_filename}"
        case _:
            local_file_path = str(Path(output or temp) / output_filename)
            output_location = local_file_path

    mime_type, file_size, file_hash = "application/octet-stream", 0, ""
    if local_file_path is not None and (found := _resolve_local_output(local_file_path)) is not None:
        # `found` is `local_file_path` plus the extension lcpencrypt appended (`.lcpdf`, `.epub`, …).
        output_location += found.removeprefix(local_file_path)
        mime_type, file_size, file_hash = _get_file_info(found)
        telemetry.PROTECTED_CONTENT_SIZE.record(file_size, {"lcp.input.format": input_format})
        span.set_attribute("lcp.protected_content.size", file_size)

    manifest_path = _write_manifest_sidecar(package, storage, storage_path, output_filename, span)

    return _Outcome(
        LCPEncryptResult(
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
        ),
        package,
        manifest_path,
    )


def _prepackage_pdf(
    cmd_args: dict[str, CliValue],
    resolved_input: str,
    temp: str,
    metadata: PublicationMetadata,
    *,
    cover: str | None,
    cover_name: str,
    span: trace.Span,
) -> tuple[str | None, PackageReport | None]:
    """Swap a raw PDF input for a Readium package carrying the metadata.

    Returns `(temp dir, report)`: the caller removes the temp dir after lcpencrypt ran; the
    report is None when nothing was packaged.

    lcpencrypt has no metadata flags, so for raw-PDF input with metadata the PDF is wrapped in a
    `.rpf` whose manifest lcpencrypt keeps. Best effort: on any failure the raw PDF is encrypted
    without the metadata rather than failing the task. Downloads (remote PDF or cover) live in
    the package's temp dir.
    """
    if metadata.is_empty() or not _looks_like_pdf(resolved_input):
        return None, None

    package_temp_dir: str | None = None
    try:
        os.makedirs(temp, exist_ok=True)
        package_temp_dir = tempfile.mkdtemp(prefix="lcp-rwpp-", dir=temp)
        local_pdf, local_cover = _localize_inputs(resolved_input, cover, package_temp_dir)
        package_path = str(Path(package_temp_dir) / f"{Path(resolved_input.split('?')[0]).stem or 'book'}.rpf")
        report = build_pdf_package(local_pdf, package_path, metadata, cover_source=local_cover, cover_name=cover_name)
        cmd_args["input"] = package_path
        span.set_attributes(
            {
                "lcp.metadata.injected": True,
                "lcp.package.cover": report.cover,
                "lcp.package.pages": report.page_count,
                "lcp.package.toc_entries": report.toc_entries,
            }
        )
        for warning in report.warnings:
            span.add_event("lcp.package.warning", {"message": warning})
        # What the reading app will show, in Tempo: the metadata block (no toc), size-capped.
        metadata_json = json.dumps(report.manifest.get("metadata", {}), ensure_ascii=False)
        span.add_event("lcp.package.manifest", {"metadata": metadata_json[:_MANIFEST_EVENT_MAX_CHARS]})
        logger.info(
            "Pre-packaged PDF into a Readium package: title=%r authors=%r cover=%s pages=%d toc=%d",
            metadata.title,
            metadata.authors,
            report.cover,
            report.page_count,
            report.toc_entries,
        )
        return package_temp_dir, report
    except Exception as error:  # noqa: BLE001 - metadata injection is best effort
        logger.warning("Failed to pre-package PDF with metadata, encrypting the raw input: %s", error)
        span.add_event("lcp.metadata.injection_failed", {"exception.message": str(error)})
        return package_temp_dir, None


def _localize_inputs(resolved_input: str, cover: str | None, temp_dir: str) -> tuple[str, str | None]:
    """Local copies of a remote PDF and cover; an unreachable cover is dropped (page 1 is rendered)."""
    local_pdf, _ = localize(resolved_input, temp_dir, ".pdf")
    local_cover: str | None = None
    if cover is not None:
        try:
            local_cover, _ = localize(cover, temp_dir, suffix_of(cover) or ".img")
        except Exception as error:  # noqa: BLE001 - the first page is rendered instead
            logger.warning("Could not fetch the catalog cover %s: %s", cover, error)
    return local_pdf, local_cover


def _write_manifest_sidecar(
    package: PackageReport | None,
    storage: str | None,
    storage_path: str,
    output_filename: str,
    span: trace.Span,
) -> str | None:
    """`{storage}/{filename}.manifest.json`: the package's (unencrypted) manifest, for inspection.

    No content and no keys, only metadata, reading order, toc and resources. Returns its path
    relative to STORAGE_PATH, or None when there is nothing to write or nowhere to write it.
    A failed write is a warning: the publication itself is fine.
    """
    directory = _local_storage_dir(storage, storage_path)
    if package is None or not package.manifest or directory is None or not _sidecar_enabled():
        return None
    name = f"{output_filename}.manifest.json"
    try:
        (directory / name).write_text(json.dumps(package.manifest, ensure_ascii=False, indent=2) + "\n")
    except OSError as error:
        logger.warning("Could not write the manifest sidecar %s: %s", directory / name, error)
        span.add_event("lcp.package.sidecar_failed", {"exception.message": str(error)})
        return None
    relative = f"{storage}/{name}"
    span.set_attribute("lcp.package.manifest_path", relative)
    return relative


def _report(params: LCPEncryptParams, task_id: str | None, outcome: _Outcome) -> None:
    """POST the outcome to the catalog's `report` URL, when the task got one.

    Whatever goes wrong here is logged and swallowed: the task's own result (or the exception
    it is about to re-raise) must never be replaced by a reporting problem.
    """
    if not (url := params.get("report")):
        return
    try:
        trace_id, _ = current_trace_ids()
        payload = encryption_report(
            outcome.result,
            contentid=params.get("contentid"),
            task_id=task_id,
            trace_id=trace_id,
            package=outcome.package,
            manifest_path=outcome.manifest_path,
        )
        send_report(url, payload)
    except Exception as error:  # noqa: BLE001 - a report must not change the task's outcome
        logger.warning("Reporting the task outcome of %s failed: %r", task_id, error)


@app.task(bind=True, name="evilflowers_lcpencrypt_worker.package_preview", queue=QUEUE)
def package_preview(
    self: "Task[Any, PackagePreviewResult]",  # noqa: UP037 - must not be evaluated at runtime, see lcpencrypt
    **params: Unpack[LCPEncryptParams],
) -> PackagePreviewResult:
    """Build the package `lcpencrypt` would get for a raw PDF, without encrypting anything.

    Takes the encryption task's parameters. Nothing reaches lcpencrypt, the License Server or
    `notify`. The result carries the manifest and the cover; with file-system `storage` both are
    also written there (`{filename}.preview.manifest.json`, `{filename}.preview.jpg`), since the
    catalog runs without a result backend.
    """
    input_file = params["input_file"]
    contentid = params.get("contentid")
    filename = params.get("filename") or contentid or Path(input_file).stem
    storage_path = str(settings().storage_path)
    temp = params.get("temp", "/tmp")  # noqa: S108 - as in lcpencrypt
    resolved_input = _resolve(input_file, storage_path)

    if not _looks_like_pdf(resolved_input):
        return _preview_failed("package preview supports PDF input only (EPUB carries its own metadata)")

    tracer = trace.get_tracer(telemetry.INSTRUMENTATION_SCOPE, telemetry.service_version())
    with tracer.start_as_current_span(
        "lcpencrypt.package_preview",
        attributes={"lcp.content_id": contentid or "", "lcp.input.format": "pdf"},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        metadata = PublicationMetadata.from_params(params)
        cover = _resolve(cover_param, storage_path) if (cover_param := params.get("cover")) else None
        try:
            os.makedirs(temp, exist_ok=True)
            temp_dir = tempfile.mkdtemp(prefix="lcp-preview-", dir=temp)
            try:
                local_pdf, local_cover = _localize_inputs(resolved_input, cover, temp_dir)
                preview = preview_pdf_package(
                    local_pdf, metadata, cover_source=local_cover, cover_name=filename, work_dir=temp_dir
                )
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as error:  # noqa: BLE001 - a preview reports, it does not crash the worker
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, type(error).__name__))
            logger.warning("Package preview failed for %s (content %s): %s", input_file, contentid, error)
            return _preview_failed(f"Package preview failed: {error}")

        report = preview.report
        warnings = list(report.warnings)
        if metadata.is_empty():
            warnings.append("no publication metadata: the encryption task lets lcpencrypt package this PDF itself")

        manifest_path, cover_path = "", ""
        if (directory := _local_storage_dir(params.get("storage"), storage_path)) is not None:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                manifest_name = f"{filename}.preview.manifest.json"
                (directory / manifest_name).write_text(json.dumps(preview.manifest, ensure_ascii=False, indent=2))
                manifest_path = f"{params.get('storage')}/{manifest_name}"
                if preview.cover is not None:
                    cover_name = f"{filename}.preview.jpg"
                    (directory / cover_name).write_bytes(preview.cover)
                    cover_path = f"{params.get('storage')}/{cover_name}"
            except OSError as error:
                warnings.append(f"could not write the preview to storage: {error}")

        span.set_attributes(
            {
                "lcp.package.cover": report.cover,
                "lcp.package.pages": report.page_count,
                "lcp.package.toc_entries": report.toc_entries,
            }
        )
        logger.info(
            "Package preview for %s: cover=%s pages=%d toc=%d",
            input_file,
            report.cover,
            report.page_count,
            report.toc_entries,
        )
        return PackagePreviewResult(
            success=True,
            error="",
            manifest=preview.manifest,
            cover_jpeg_base64=base64.b64encode(preview.cover).decode() if preview.cover is not None else "",
            cover=report.cover,
            pages=report.page_count,
            toc_entries=report.toc_entries,
            warnings=warnings,
            manifest_path=manifest_path,
            cover_path=cover_path,
        )


def _preview_failed(error: str) -> PackagePreviewResult:
    return PackagePreviewResult(
        success=False,
        error=error,
        manifest={},
        cover_jpeg_base64="",
        cover="none",
        pages=0,
        toc_entries=0,
        warnings=[],
        manifest_path="",
        cover_path="",
    )


def _failed(params: LCPEncryptParams, error: str) -> LCPEncryptResult:
    return LCPEncryptResult(
        content_id=params.get("contentid") or "unknown",
        content_encryption_key="",
        storage_mode=_determine_storage_mode(params.get("storage")),
        protected_content_location="",
        protected_content_disposition=Path(params["input_file"]).name,
        protected_content_type="",
        protected_content_length=0,
        protected_content_sha256="",
        success=False,
        # lcpencrypt's output can echo the -lcpsv/-notify URLs; the error is stored by the catalog and in task events.
        error=logs.redact(error),
    )


def _record(span: trace.Span, input_format: str, outcome: str, seconds: float) -> None:
    attributes = {"lcp.input.format": input_format, "lcp.outcome": outcome}
    telemetry.ENCRYPTION_DURATION.record(seconds, attributes)
    telemetry.ENCRYPTIONS.add(1, attributes)
    span.set_attribute("lcp.outcome", outcome)


__all__ = ["LCPEncryptParams", "LCPEncryptResult", "PackagePreviewResult", "app", "lcpencrypt", "package_preview"]
