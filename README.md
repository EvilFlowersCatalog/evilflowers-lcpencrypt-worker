# evilflowers-lcpencrypt-worker

A modern, type-safe Celery worker for encrypting publications using the Readium LCP (Licensed Content Protection)
standard. This worker wraps the `lcpencrypt` command-line tool and provides a robust async task interface with
comprehensive error handling and result tracking.

## Features

- **Modern Python typing**: Full type hints using TypedDict and modern Python 3.12+ syntax
- **Comprehensive result tracking**: Returns detailed encryption results including file metadata and encryption keys
- **Multiple storage modes**: Support for S3, filesystem, and legacy storage modes
- **Format support**: EPUB 2/3, PDF, Audiobooks, Readium Packages, W3C Audiobooks
- **OpenTelemetry integration**: Optional distributed tracing support
- **Proper error handling**: Structured exceptions with detailed error information
- **Production-ready**: Task acknowledgment, worker loss detection, and retry configuration

## Installation

### Using Docker

Build the Docker image:

```bash
docker build -t evilflowers-lcpencrypt-worker .
```

Run the container:

```bash
docker run -d --name lcpencrypt-worker \
    -e BROKER=redis://your-redis-server:6379/0 \
    -e STORAGE_PATH=/mnt/data \
    -e READIUM_LCPENCRYPT_BIN=/usr/local/bin/lcpencrypt \
    evilflowers-lcpencrypt-worker
```

### Using Poetry

```bash
poetry install

# Run using the built-in entry point (recommended)
poetry run python -m evilflowers_lcpencrypt_worker

# Or specify custom queue
poetry run python -m evilflowers_lcpencrypt_worker -Q high_priority

# Or use the traditional celery command
poetry run celery -A evilflowers_lcpencrypt_worker worker --loglevel=info
```

### Using Python Module

The worker can be started as a Python module with various options:

```bash
# Basic usage
python -m evilflowers_lcpencrypt_worker

# Specify queue
python -m evilflowers_lcpencrypt_worker -Q custom_queue

# With custom settings
python -m evilflowers_lcpencrypt_worker \
    --queue high_priority \
    --loglevel debug \
    --concurrency 4 \
    --prefetch-multiplier 2

# With autoscaling
python -m evilflowers_lcpencrypt_worker \
    --queue lcpencrypt \
    --autoscale 10,3 \
    --max-tasks-per-child 1000
```

Available options:
- `-Q, --queue`: Queue name (default: `evilflowers_lcpencrypt_worker`)
- `-l, --loglevel`: Logging level (default: `info`)
- `-c, --concurrency`: Number of worker processes
- `-n, --hostname`: Custom hostname
- `-P, --pool`: Pool implementation (default: `prefork`)
- `--prefetch-multiplier`: Tasks to prefetch per worker (default: 1)
- `--max-tasks-per-child`: Max tasks before worker restart
- `--autoscale`: Enable autoscaling (format: `MAX,MIN`)

## Usage

### Basic Example

```python
from evilflowers_lcpencrypt_worker import lcpencrypt

# Encrypt a publication with S3 storage
result = lcpencrypt.delay(
    input_file="book.epub",
    storage="s3:eu-west-3:lcp-storage",
    url="https://lcp-storage.s3.eu-west-3.amazonaws.com",
    contentid="book-123",
    filename="book-123.epub",
    verbose=True
)

# Get the result
encryption_result = result.get()
print(f"Success: {encryption_result['success']}")
print(f"Content ID: {encryption_result['content_id']}")
print(f"Location: {encryption_result['protected_content_location']}")
print(f"SHA-256: {encryption_result['protected_content_sha256']}")
```

### Filesystem Storage

```python
result = lcpencrypt.delay(
    input_file="document.pdf",
    storage="encrypted",
    url="https://cdn.example.com/encrypted",
    contentid="doc-456",
    lcpsv="http://user:pass@lcp-server.com:8989",
    notify="http://user:pass@cms.example.com/notify"
)
```

### Legacy Mode

```python
result = lcpencrypt.delay(
    input_file="publication.epub",
    output="temp_encrypted",
    contentid="pub-789",
    lcpsv="http://lcp-server.com:8989",
    login="admin",
    password="secret"
)
```

### Using Celery Signature

```python
from celery import signature

task = signature("evilflowers_lcpencrypt_worker.lcpencrypt", kwargs={
    "input_file": "book.epub",
    "storage": "s3:us-east-1:my-bucket",
    "url": "https://my-bucket.s3.us-east-1.amazonaws.com",
    "contentid": "unique-id-123",
    "verbose": True
})

result = task.apply_async()
```

## Parameters

### Required Parameters

| Parameter    | Type | Description                                             |
|--------------|------|---------------------------------------------------------|
| `input_file` | str  | Path to source file (relative to `STORAGE_PATH` or URL) |

### Recommended Mode Parameters

Use these for modern deployments with custom storage:

| Parameter   | Type | Description                                                            |
|-------------|------|------------------------------------------------------------------------|
| `storage`   | str  | Target storage location. Format: `s3:region:bucket` or filesystem path |
| `url`       | str  | Public base URL corresponding to storage location                      |
| `filename`  | str  | Output filename (optional, uses `contentid` if omitted)                |
| `contentid` | str  | Unique content identifier (optional, UUID generated if omitted)        |

### Optional Parameters

| Parameter | Type | Default | Description                                                 |
|-----------|------|---------|-------------------------------------------------------------|
| `temp`    | str  | `/tmp`  | Working directory for temporary files                       |
| `lcpsv`   | str  | -       | License server endpoint (format: `http://user:pass@host`)   |
| `notify`  | str  | -       | CMS notification endpoint (format: `http://user:pass@host`) |
| `verbose` | bool | `False` | Enable verbose logging                                      |

### Legacy Mode Parameters

For compatibility with older LCP server deployments:

| Parameter  | Type | Description                            |
|------------|------|----------------------------------------|
| `output`   | str  | Temporary output directory             |
| `login`    | str  | License server login                   |
| `password` | str  | License server password                |

## Return Value

The task returns an `LCPEncryptResult` TypedDict:

```python
{
    "content_id": str,                    # Content identifier
    "content_encryption_key": str,        # Encryption key used
    "storage_mode": 0 | 1 | 2,           # 0=not stored, 1=S3, 2=filesystem
    "protected_content_location": str,    # URL or path to encrypted file
    "protected_content_disposition": str, # Original filename
    "protected_content_type": str,        # MIME type (e.g., application/epub+zip)
    "protected_content_length": int,      # File size in bytes
    "protected_content_sha256": str,      # SHA-256 hash of encrypted file
    "success": bool,                      # Operation success status
    "error": str                          # Error message (only if success=False)
}
```

## Environment Variables

| Variable                      | Description                      | Default                         | Example                     |
|-------------------------------|----------------------------------|---------------------------------|-----------------------------|
| `BROKER`                      | Celery broker URL                | `redis://localhost:6379/0`      | `redis://redis:6379/0`      |
| `LOG_LEVEL`                   | Logging level                    | `INFO`                          | `DEBUG`                     |
| `STORAGE_PATH`                | Base path for file storage       | `/mnt/data`                     | `/var/lib/lcp/storage`      |
| `READIUM_LCPENCRYPT_BIN`      | Path to `lcpencrypt` binary      | `lcpencrypt`                    | `/usr/local/bin/lcpencrypt` |
| `OTEL_SERVICE_NAME`           | Service name for OpenTelemetry   | `evilflowers-lcpencrypt-worker` | -                           |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OpenTelemetry Collector endpoint | -                               | `http://collector:4317`     |

## Supported Formats

The worker supports encryption of the following publication formats:

- **EPUB 2 and EPUB 3** (including embedded multimedia)
- **PDF** documents
- **Readium Packages** ([spec](https://readium.org/webpub-manifest/packaging.html))
  - Audiobooks ([spec](https://readium.org/webpub-manifest/profiles/audiobook.html))
  - Digital Visual Narratives ([spec](https://readium.org/webpub-manifest/profiles/divina.html))
  - PDF embedded in Readium Packages
  - HTML-based Publications
- **W3C Audiobooks** packaged as LPF ([spec](https://www.w3.org/TR/lpf/))

## Storage Modes

### S3 Storage

Store encrypted publications in Amazon S3 buckets:

```python
storage="s3:eu-west-3:lcp-storage"
url="https://lcp-storage.s3.eu-west-3.amazonaws.com"
```

### Filesystem Storage

Store encrypted publications in a network drive or local filesystem:

```python
storage="encrypted_books"
url="https://cdn.example.com/lcp"
```

The worker will create the directory at `{STORAGE_PATH}/{storage}` if it doesn't exist.

### Legacy Mode

For legacy LCP server deployments, omit `storage` and `url`:

```python
output="temp_output"
login="admin"
password="secret"
```

## Development

### Type Checking

The project uses modern Python typing with TypedDict:

```python
from evilflowers_lcpencrypt_worker import LCPEncryptParams, LCPEncryptResult

# Type-safe parameters
params: LCPEncryptParams = {
    "input_file": "book.epub",
    "storage": "s3:eu-west-3:bucket",
    "url": "https://bucket.s3.eu-west-3.amazonaws.com"
}

# Type-safe result
result: LCPEncryptResult = lcpencrypt.delay(**params).get()
```

### Testing

```bash
poetry run pytest
```

### Code Formatting

```bash
poetry run black evilflowers_lcpencrypt_worker/
```

## Architecture

```
evilflowers_lcpencrypt_worker/
├── __init__.py          # Main Celery task and app
├── __main__.py          # CLI entry point for starting worker
├── types.py             # TypedDict definitions
└── helpers.py           # Executable runner utilities
```

## Error Handling

The worker provides comprehensive error handling:

- Returns `success: False` with error details if encryption fails
- Logs detailed error information including stdout/stderr
- Raises `ExecutableException` with structured error data
- Supports Celery task retry mechanisms

## License

This open-source project is maintained by students and PhD candidates at the [Faculty of Informatics and Information Technologies](https://www.fiit.stuba.sk/), Slovak University of Technology.

## Related Documentation

- [LCP Encryption Tool Documentation](docs/Encryption-tool.md)
- [Readium LCP Server](https://github.com/readium/readium-lcp-server)
- [LCP Specification](https://www.edrlab.org/readium-lcp/)

---

![FIIT STU](docs/images/fiit.png)
