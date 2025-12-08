"""Type definitions for LCP encryption worker."""

from typing import Literal, TypedDict


class LCPEncryptParams(TypedDict, total=False):
    """Parameters for LCP encryption task.

    Attributes:
        input_file: Path to the source file (required). Can be a file system path or http(s) URL.
        storage: Target location for encrypted publication. Either an S3 bucket (s3:region:bucket)
                 or file system path. If omitted, uses legacy output mode.
        url: Base URL associated with the storage. Must be a public URL corresponding to storage location.
        contentid: Unique identifier of the encrypted publication. If omitted, a UUID is generated.
        filename: File name of the encrypted publication. If omitted, contentid is used as filename.
        temp: Working folder for temporary files. Defaults to '/tmp'.
        lcpsv: Host name of the License Server to be notified. Format: http://username:password@example.com
        notify: Notification endpoint of a CMS. Format: http://username:password@example.com
        verbose: If True, information sent to LCP Server and CMS will be logged.
        output: (Legacy mode) Temporary location of encrypted publication without filename.
        login: (Legacy mode) Login for license server. Required if lcpsv is used in legacy mode.
        password: (Legacy mode) Password for license server. Required if lcpsv is used in legacy mode.
    """

    # Required parameter
    input_file: str

    # Recommended mode parameters
    storage: str
    url: str

    # Optional parameters
    contentid: str
    filename: str
    temp: str
    lcpsv: str
    notify: str
    verbose: bool

    # Legacy mode parameters
    output: str
    login: str
    password: str


StorageMode = Literal[0, 1, 2]
"""Storage mode indication: 0 = not stored yet, 1 = stored on S3, 2 = stored in file system."""


class LCPEncryptResult(TypedDict):
    """Result of LCP encryption operation.

    This structure matches the JSON payload sent to the license server.

    Attributes:
        content_id: Content identifier.
        content_encryption_key: Content encryption key used for encryption.
        storage_mode: Indication of how content is stored (0=not stored, 1=S3, 2=filesystem).
        protected_content_location: Absolute URL or temporary file path of encrypted publication.
        protected_content_disposition: Original file name of the encrypted publication.
        protected_content_type: Media type of the encrypted content (e.g., application/epub+zip).
        protected_content_length: Size of the encrypted content in bytes.
        protected_content_sha256: SHA-256 hash of the encrypted content.
        success: Whether the encryption operation completed successfully.
        error: Error message if operation failed (only present if success is False).
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
    error: str  # Only present when success is False
