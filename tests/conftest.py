import pytest


@pytest.fixture
def tmp_storage(tmp_path):
    """Provide a temporary storage directory simulating STORAGE_PATH."""
    return str(tmp_path)


@pytest.fixture
def sample_epub(tmp_path):
    """Create a minimal sample EPUB file for testing."""
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"PK\x03\x04fake-epub-content-for-testing")
    return str(epub_path)


@pytest.fixture
def sample_pdf(tmp_path):
    """Create a minimal sample PDF file for testing."""
    pdf_path = tmp_path / "document.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake-pdf-content-for-testing")
    return str(pdf_path)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure environment variables don't leak between tests."""
    for var in ["BROKER", "REDIS_URL", "STORAGE_PATH", "READIUM_LCPENCRYPT_BIN", "MANIFEST_SIDECAR", "MAX_DELIVERIES"]:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def no_delivery_counter(monkeypatch):
    """In-process task runs count no deliveries in Redis: every run is the first (`tests/test_deliveries.py` sets
    its own counter)."""
    from evilflowers_worker import runtime

    import evilflowers_lcpencrypt_worker  # noqa: F401 - create_app runs on import

    monkeypatch.setattr(runtime.runtime().deliveries, "client", None)
