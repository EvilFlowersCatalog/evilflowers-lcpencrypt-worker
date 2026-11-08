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
        url: Public base URL that corresponds to `storage`.
        contentid: Unique identifier of the encrypted publication (lcpencrypt generates a UUID
            when omitted).
        filename: File name of the encrypted publication, without extension (defaults to contentid).
        title: Publication title. For raw-PDF input it is injected into the Readium package
            manifest so reading apps (e.g. Thorium) display it. Ignored for EPUB (OPF wins).
        author: Publication author, injected like `title`. Ignored for EPUB.
        temp: Working folder for temporary files (default `/tmp`).
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
    title: str
    author: str
    temp: str
    lcpsv: str
    notify: str
    verbose: bool

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
