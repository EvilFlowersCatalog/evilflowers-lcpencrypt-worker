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
        notify: CMS notification endpoint, `http://user:pass@host`.
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


__all__ = ["LCPEncryptParams", "LCPEncryptResult", "StorageMode"]
