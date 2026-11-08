"""Type definitions for the LCP encryption worker.

These describe the Celery wire contract with EvilFlowersCatalog
(`apps/readium/services/content_encryption_service.py`): the kwargs the catalog sends and the
JSON result. Field names and value types must not change without a matching catalog change.
"""

from typing import Literal, Required, TypedDict

type StorageMode = Literal[0, 1, 2]
"""How content is stored: 0 = not stored yet, 1 = S3, 2 = file system. Serialized as an int."""


class LCPEncryptParams(TypedDict, total=False):
    """Keyword arguments of the `evilflowers_lcpencrypt_worker.lcpencrypt` task.

    Attributes:
        input_file: Source file (required): a path relative to STORAGE_PATH, or an http(s) URL.
        storage: Target location of the encrypted publication: an S3 bucket (`s3:region:bucket`)
            or a file system path relative to STORAGE_PATH. Omitted = legacy output mode.
        url: Public base URL that corresponds to `storage`. Required with `storage`
            (lcpencrypt refuses `storage` without it).
        contentid: Unique identifier of the encrypted publication (lcpencrypt generates a UUID
            when omitted).
        filename: File name of the encrypted publication, without extension (defaults to contentid).
        temp: Working folder for temporary files (default `/tmp`).

    Publication metadata (raw-PDF input only; EPUB carries its own OPF metadata). When any of
    these is given, the PDF is wrapped into a Readium package whose manifest carries them, so
    reading apps (Thorium, …) display them. Empty values are ignored.
        title: Title.
        subtitle: Subtitle.
        author: One author (kept for catalogs that send a single name).
        authors: All authors, in order; wins over `author`.
        translators, editors, illustrators, contributors: Further contributors by role.
        publisher: Publisher name.
        language: BCP 47 language code(s), e.g. `sk` or `["sk", "en"]`.
        published: Publication date, full `YYYY-MM-DD` (a partial `2023` or `2023-05` is
            dropped: lcpencrypt cannot parse it).
        description: Summary.
        subjects: Subject headings / categories.
        identifier: Publication identifier, preferably a URI (`urn:isbn:…`, `https://doi.org/…`).
        cover: Cover image, a path relative to STORAGE_PATH or an http(s) URL. Without it the
            first page of the PDF is rendered, as lcpencrypt does.
        lcpsv: License Server to notify, `http://user:pass@host`.
        notify: CMS notification endpoint, `http://user:pass@host`. lcpencrypt calls it on
            success only.
        report: Catalog endpoint the worker POSTs an `EncryptionReport` to after every run,
            failures included, `http://user:pass@host/path` (Basic auth, like `notify`).
        verbose: Log what is sent to the License Server and the CMS.
        output: (Legacy mode) Temporary location of the encrypted publication, without file name.
        login: (Legacy mode) License Server login.
        password: (Legacy mode) License Server password.
    """

    input_file: Required[str]

    # Recommended mode
    storage: str
    url: str

    # Optional
    contentid: str
    filename: str
    temp: str
    lcpsv: str
    notify: str
    report: str
    verbose: bool

    # Publication metadata (raw-PDF input)
    title: str
    subtitle: str
    author: str
    authors: list[str]
    translators: list[str]
    editors: list[str]
    illustrators: list[str]
    contributors: list[str]
    publisher: str
    language: str | list[str]
    published: str
    description: str
    subjects: list[str]
    identifier: str
    cover: str

    # Legacy mode
    output: str
    login: str
    password: str


class LCPEncryptResult(TypedDict):
    """Result of an LCP encryption run (the JSON payload shape of the License Server).

    Attributes:
        content_id: Content identifier.
        content_encryption_key: Content encryption key, when lcpencrypt printed it.
        storage_mode: 0 = not stored, 1 = S3, 2 = file system.
        protected_content_location: Absolute URL or file path of the encrypted publication.
        protected_content_disposition: Original file name of the source publication.
        protected_content_type: Media type of the encrypted content.
        protected_content_length: Size of the encrypted content in bytes.
        protected_content_sha256: SHA-256 of the encrypted content.
        success: Whether the encryption completed.
        error: Error message; empty when `success` is True.
    """

    content_id: str
    content_encryption_key: str
    storage_mode: StorageMode
    protected_content_location: str
    protected_content_disposition: str
    protected_content_type: str
    protected_content_length: int
    protected_content_sha256: str
    success: bool
    error: str


class PackageSummary(TypedDict, total=False):
    """What the worker put into the package and where the result went (`report` of `EncryptionReport`).

    Package keys are present only when the worker built the package (PDF input with metadata);
    `protected_content_*` only after a successful run.

    Attributes:
        cover: `catalog` (the catalog's `cover`), `rendered` (page 1) or `none`.
        pages: Page count (`numberOfPages`).
        toc_entries: Number of `toc` entries taken from the PDF outline, nested ones included.
        warnings: Best-effort steps that fell back (unusable catalog cover, unreadable PDF, …).
        manifest: The manifest sidecar, relative to STORAGE_PATH (`{storage}/{filename}.manifest.json`).
        protected_content_location: As in `LCPEncryptResult`.
        protected_content_type: As in `LCPEncryptResult`.
        protected_content_length: As in `LCPEncryptResult`.
        protected_content_sha256: As in `LCPEncryptResult`.
    """

    cover: str
    pages: int
    toc_entries: int
    warnings: list[str]
    manifest: str
    protected_content_location: str
    protected_content_type: str
    protected_content_length: int
    protected_content_sha256: str


class EncryptionReport(TypedDict):
    """JSON body the worker POSTs to the `report` URL once per task, whatever the outcome.

    Never carries the content encryption key. The request has a W3C `traceparent` header, so
    the catalog's handler joins the task's trace.

    Attributes:
        contentid: The task's `contentid` (empty when the catalog sent none).
        task_id: Celery task id.
        trace_id: Trace of the task (lowercase hex), empty without telemetry.
        success: Whether the publication was encrypted and lcpencrypt did not withdraw it.
        error: Why not; empty when `success` is True.
        report: See `PackageSummary`.
    """

    contentid: str
    task_id: str
    trace_id: str
    success: bool
    error: str
    report: PackageSummary


class PackagePreviewResult(TypedDict):
    """Result of `evilflowers_lcpencrypt_worker.package_preview` (nothing is encrypted or registered).

    Attributes:
        success: Whether the package could be built.
        error: Why not; empty when `success` is True.
        manifest: The `manifest.json` the encryption task would hand to lcpencrypt.
        cover_jpeg_base64: The cover entry of the package, base64; empty without a cover.
        cover: `catalog`, `rendered` or `none`.
        pages: Page count.
        toc_entries: `toc` entries, nested ones included.
        warnings: Best-effort steps that fell back.
        manifest_path: Where the preview manifest was written, relative to STORAGE_PATH
            (`{storage}/{filename}.preview.manifest.json`); empty without filesystem `storage`.
        cover_path: Where the preview cover was written (`{storage}/{filename}.preview.jpg`), or empty.
    """

    success: bool
    error: str
    manifest: dict[str, object]
    cover_jpeg_base64: str
    cover: str
    pages: int
    toc_entries: int
    warnings: list[str]
    manifest_path: str
    cover_path: str


__all__ = [
    "EncryptionReport",
    "LCPEncryptParams",
    "LCPEncryptResult",
    "PackagePreviewResult",
    "PackageSummary",
    "StorageMode",
]
