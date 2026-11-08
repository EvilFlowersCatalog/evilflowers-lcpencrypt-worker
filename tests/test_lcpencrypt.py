"""Tests for the lcpencrypt Celery task and helper functions."""

import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Final
from unittest.mock import patch

import pytest
from PIL import Image

from evilflowers_lcpencrypt_worker import (
    _determine_storage_mode,
    _get_file_info,
    _looks_like_pdf,
    lcpencrypt,
    package_preview,
)
from evilflowers_lcpencrypt_worker.helpers import ExecutableException, ExecutableResult
from tests.pdf_factory import Bookmark, write_pdf


class TestDetermineStorageMode:
    def test_none_returns_0(self):
        assert _determine_storage_mode(None) == 0

    def test_empty_string_returns_0(self):
        assert _determine_storage_mode("") == 0

    def test_s3_returns_1(self):
        assert _determine_storage_mode("s3:eu-west-3:my-bucket") == 1

    def test_s3_prefix_only_returns_1(self):
        assert _determine_storage_mode("s3:") == 1

    def test_filesystem_path_returns_2(self):
        assert _determine_storage_mode("/mnt/data/storage") == 2

    def test_relative_path_returns_2(self):
        assert _determine_storage_mode("catalogs/my-catalog/entry-uuid") == 2


class TestGetFileInfo:
    def test_epub_file(self, tmp_path):
        epub = tmp_path / "test.epub"
        content = b"PK\x03\x04fake-epub"
        epub.write_bytes(content)

        mime_type, size, sha256 = _get_file_info(str(epub))

        assert mime_type == "application/epub+zip"
        assert size == len(content)
        assert sha256 == hashlib.sha256(content).hexdigest()

    def test_pdf_file(self, tmp_path):
        pdf = tmp_path / "test.pdf"
        content = b"%PDF-1.4 fake-pdf"
        pdf.write_bytes(content)

        mime_type, size, sha256 = _get_file_info(str(pdf))

        assert mime_type == "application/pdf"
        assert size == len(content)
        assert sha256 == hashlib.sha256(content).hexdigest()

    def test_audiobook_extension(self, tmp_path):
        audiobook = tmp_path / "test.audiobook"
        content = b"audiobook-content"
        audiobook.write_bytes(content)

        mime_type, _size, _sha256 = _get_file_info(str(audiobook))

        assert mime_type == "application/audiobook+zip"

    def test_sha256_correctness(self, tmp_path):
        f = tmp_path / "hash_test.bin"
        content = b"test content for hashing"
        f.write_bytes(content)

        _, _, sha256 = _get_file_info(str(f))

        expected = hashlib.sha256(content).hexdigest()
        assert sha256 == expected

    def test_large_file_chunked_reading(self, tmp_path):
        """Verify SHA-256 works for files larger than the 8192-byte chunk size."""
        f = tmp_path / "large.bin"
        content = b"x" * 20000
        f.write_bytes(content)

        _, size, sha256 = _get_file_info(str(f))

        assert size == 20000
        assert sha256 == hashlib.sha256(content).hexdigest()

    def test_returns_three_values(self, tmp_path):
        f = tmp_path / "test.pdf"
        f.write_bytes(b"content")

        result = _get_file_info(str(f))

        assert len(result) == 3
        mime_type, size, sha256 = result
        assert isinstance(mime_type, str)
        assert isinstance(size, int)
        assert isinstance(sha256, str)


class TestGetFileInfoLcpExtensions:
    """The encrypted outputs use LCP-specific extensions unknown to mimetypes."""

    @pytest.mark.parametrize(
        "ext,expected_mime",
        [
            (".lcpdf", "application/pdf+lcp"),
            (".lcpa", "application/audiobook+lcp"),
            (".lcpdi", "application/divina+lcp"),
            (".webpub", "application/webpub+lcp"),
        ],
    )
    def test_lcp_extension_mime_types(self, tmp_path, ext, expected_mime):
        f = tmp_path / f"encrypted{ext}"
        f.write_bytes(b"encrypted-content")

        mime_type, _, _ = _get_file_info(str(f))

        assert mime_type == expected_mime


class TestLooksLikePdf:
    def test_local_pdf_path(self):
        assert _looks_like_pdf("catalogs/x/book.pdf") is True

    def test_uppercase_extension(self):
        assert _looks_like_pdf("/mnt/data/BOOK.PDF") is True

    def test_epub_is_not_pdf(self):
        assert _looks_like_pdf("book.epub") is False

    def test_http_url_with_query_string(self):
        assert _looks_like_pdf("https://example.com/files/book.pdf?token=abc") is True

    def test_http_url_non_pdf(self):
        assert _looks_like_pdf("https://example.com/files/book.epub") is False


def _call_lcpencrypt(**kwargs):
    """Call the lcpencrypt task directly (Celery handles self binding)."""
    return lcpencrypt.run(**kwargs)


def _capture_package(holder):
    """Return a run_executable side_effect that snapshots the input package.

    The temporary Readium package is deleted after the task returns, so we read its
    manifest while the binary is 'running' (before cleanup) and stash it in ``holder``.
    """

    def _side_effect(*args, **kwargs):
        input_path = kwargs["kwargs_dict"]["input"]
        holder["input"] = input_path
        if input_path.endswith(".rpf") and Path(input_path).exists():
            holder["temp_entries"] = sorted(path.name for path in Path(input_path).parent.iterdir())
            with zipfile.ZipFile(input_path) as archive:
                holder["names"] = archive.namelist()
                holder["manifest"] = json.loads(archive.read("manifest.json"))
                holder["files"] = {name: archive.read(name) for name in holder["names"] if name.endswith(".jpg")}
        return ExecutableResult(returncode=0, stdout="", stderr="")

    return _side_effect


class TestLcpencryptTask:
    """Tests for the lcpencrypt Celery task.

    All tests mock the subprocess execution and file system interactions
    to test the task logic independently of the lcpencrypt binary.
    """

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_catalog_style_filesystem_storage(self, mock_run, tmp_path, monkeypatch):
        """The parameters EvilFlowersCatalog sends (`_queue_encryption_task`, develop)."""
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        entry_dir = tmp_path / "catalogs" / "my-catalog" / "entry-uuid"
        source_dir = entry_dir / "encrypted" / "source" / "abc-123-uuid"
        source_dir.mkdir(parents=True)
        write_pdf(source_dir / "book.pdf", pages=2)

        def run(*args, **kwargs):
            # lcpencrypt writes `<filename>.lcpdf` into the storage directory
            (entry_dir / "encrypted" / "abc-123-uuid.lcpdf").write_bytes(b"PK-encrypted")
            return _capture_package(holder)(*args, **kwargs)

        holder: dict = {}
        mock_run.side_effect = run

        result = _call_lcpencrypt(
            input_file="catalogs/my-catalog/entry-uuid/encrypted/source/abc-123-uuid/book.pdf",
            contentid="abc-123-uuid",
            storage="catalogs/my-catalog/entry-uuid/encrypted",
            filename="abc-123-uuid",
            lcpsv="http://user:pass@readium:8989",
            notify="http://user:pass@django:8000/api/readium/hooks/encryption",
            url="https://elvira.example/readium/v1/content",
            title="The Book",
            author="Ada Lovelace",
            temp=str(tmp_path / "tmp"),
        )

        assert result["success"] is True
        assert result["content_id"] == "abc-123-uuid"
        assert result["storage_mode"] == 2  # filesystem
        # What lcpencrypt registers: url + the file it wrote, extension included.
        assert result["protected_content_location"] == "https://elvira.example/readium/v1/content/abc-123-uuid.lcpdf"
        assert result["protected_content_type"] == "application/pdf+lcp"
        assert result["protected_content_length"] == len(b"PK-encrypted")

        cmd_args = mock_run.call_args.kwargs["kwargs_dict"]
        assert cmd_args["storage"] == str(entry_dir / "encrypted")
        assert cmd_args["url"] == "https://elvira.example/readium/v1/content"
        assert cmd_args["filename"] == "abc-123-uuid"
        assert cmd_args["lcpsv"] == "http://user:pass@readium:8989"
        assert cmd_args["notify"] == "http://user:pass@django:8000/api/readium/hooks/encryption"
        # Issue #2: the package carries the cover (named like lcpencrypt names it) and the page count.
        assert cmd_args["input"].endswith(".rpf")
        assert set(holder["names"]) == {"publication.pdf", "abc-123-uuid.jpg", "manifest.json"}
        assert holder["manifest"]["metadata"]["numberOfPages"] == 2
        assert holder["manifest"]["metadata"]["author"] == "Ada Lovelace"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_filesystem_storage_with_url(self, mock_run, tmp_path, monkeypatch):
        """Test filesystem storage with an explicit URL."""
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()
        output_file = storage_dir / "book-123.epub"
        output_file.write_bytes(b"PK\x03\x04encrypted-epub")

        mock_run.return_value = ExecutableResult(
            returncode=0,
            stdout="Content encryption key: key123\n",
            stderr="",
        )

        result = _call_lcpencrypt(
            input_file="books/input.epub",
            storage="storage",
            url="https://cdn.example.com/content",
            contentid="book-123",
            filename="book-123.epub",
        )

        assert result["success"] is True
        assert result["protected_content_location"] == "https://cdn.example.com/content/book-123.epub"
        assert result["content_encryption_key"] == "key123"
        assert result["storage_mode"] == 2

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_s3_storage_mode(self, mock_run, monkeypatch):
        """Test S3 storage mode."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        mock_run.return_value = ExecutableResult(
            returncode=0,
            stdout="Content encryption key: s3key456\n",
            stderr="",
        )

        result = _call_lcpencrypt(
            input_file="https://example.com/books/book.epub",
            storage="s3:eu-west-3:lcp-storage",
            url="https://lcp-storage.s3.eu-west-3.amazonaws.com",
            contentid="s3-book-id",
            filename="s3-book-id.epub",
        )

        assert result["success"] is True
        assert result["storage_mode"] == 1  # S3
        assert result["content_encryption_key"] == "s3key456"
        assert "lcp-storage.s3.eu-west-3.amazonaws.com" in result["protected_content_location"]

        # Verify S3 storage passed directly (not prepended with STORAGE_PATH)
        cmd_args = mock_run.call_args.kwargs.get("kwargs_dict") or mock_run.call_args[1].get("kwargs_dict")
        assert cmd_args["storage"] == "s3:eu-west-3:lcp-storage"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_legacy_mode(self, mock_run, tmp_path, monkeypatch):
        """Test legacy mode with output/login/password parameters."""
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        mock_run.return_value = ExecutableResult(
            returncode=0,
            stdout="Content encryption key: legacykey\n",
            stderr="",
        )

        result = _call_lcpencrypt(
            input_file="books/book.epub",
            output="temp_output",
            login="admin",
            password="secret",
            lcpsv="http://lcpsv:8989",
        )

        assert result["success"] is True
        assert result["storage_mode"] == 0  # not stored (no storage param)

        cmd_args = mock_run.call_args.kwargs.get("kwargs_dict") or mock_run.call_args[1].get("kwargs_dict")
        assert cmd_args["output"] == str(tmp_path / "temp_output")
        assert cmd_args["login"] == "admin"
        assert cmd_args["password"] == "secret"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_http_input_not_prepended(self, mock_run, monkeypatch):
        """Test that HTTP URLs are passed directly, not prepended with STORAGE_PATH."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        _call_lcpencrypt(
            input_file="https://example.com/books/book.epub",
            contentid="test",
        )

        cmd_args = mock_run.call_args.kwargs.get("kwargs_dict") or mock_run.call_args[1].get("kwargs_dict")
        assert cmd_args["input"] == "https://example.com/books/book.epub"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_local_input_prepended_with_storage_path(self, mock_run, monkeypatch):
        """Test that local paths are prepended with STORAGE_PATH."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        _call_lcpencrypt(
            input_file="catalogs/catalog/entry/book.pdf",
            contentid="test",
        )

        cmd_args = mock_run.call_args.kwargs.get("kwargs_dict") or mock_run.call_args[1].get("kwargs_dict")
        assert cmd_args["input"] == "/mnt/data/catalogs/catalog/entry/book.pdf"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_encryption_failure_returns_error(self, mock_run, monkeypatch):
        """Test that execution failures return a structured error result."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        mock_run.side_effect = ExecutableException(
            executable_path="lcpencrypt",
            returncode=1,
            stdout="",
            stderr="encryption error: invalid epub",
            command="lcpencrypt -input=/mnt/data/book.epub",
        )

        result = _call_lcpencrypt(
            input_file="book.epub",
            contentid="fail-test",
        )

        assert result["success"] is False
        assert "Encryption failed" in result["error"]
        assert result["content_id"] == "fail-test"
        assert result["content_encryption_key"] == ""
        assert result["protected_content_length"] == 0

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_error_without_contentid(self, mock_run, monkeypatch):
        """Test error result when contentid is not provided."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        mock_run.side_effect = ExecutableException("lcpencrypt", 1, "", "fail")

        result = _call_lcpencrypt(input_file="book.epub")

        assert result["success"] is False
        assert result["content_id"] == "unknown"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_content_key_extraction(self, mock_run, monkeypatch):
        """Test extraction of content encryption key from stdout."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        mock_run.return_value = ExecutableResult(
            returncode=0,
            stdout="Processing book.epub\nContent Encryption Key: abcdef123456\nDone.\n",
            stderr="",
        )

        result = _call_lcpencrypt(
            input_file="book.epub",
            contentid="test",
        )

        assert result["content_encryption_key"] == "abcdef123456"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_content_key_missing(self, mock_run, monkeypatch):
        """Test that empty key is returned when not present in output."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        mock_run.return_value = ExecutableResult(
            returncode=0,
            stdout="Processing done\n",
            stderr="",
        )

        result = _call_lcpencrypt(
            input_file="book.epub",
            contentid="test",
        )

        assert result["content_encryption_key"] == ""

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_file_info_populated_for_local_files(self, mock_run, tmp_path, monkeypatch):
        """Test that MIME type, size, and hash are populated for local files."""
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()
        output_file = storage_dir / "test-id.epub"
        content = b"PK\x03\x04encrypted-epub-content"
        output_file.write_bytes(content)

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        result = _call_lcpencrypt(
            input_file="input.epub",
            storage="storage",
            url="https://cdn.example.com",
            contentid="test-id",
            filename="test-id.epub",
        )

        assert result["success"] is True
        assert result["protected_content_type"] == "application/epub+zip"
        assert result["protected_content_length"] == len(content)
        assert result["protected_content_sha256"] == hashlib.sha256(content).hexdigest()

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_file_info_defaults_when_file_missing(self, mock_run, tmp_path, monkeypatch):
        """Test defaults when encrypted file doesn't exist locally."""
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        result = _call_lcpencrypt(
            input_file="input.epub",
            storage="storage",
            url="https://cdn.example.com",
            contentid="missing-file",
        )

        assert result["success"] is True
        assert result["protected_content_type"] == "application/octet-stream"
        assert result["protected_content_length"] == 0
        assert result["protected_content_sha256"] == ""

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_verbose_flag_passed(self, mock_run, monkeypatch):
        """Test that verbose=True adds the verbose flag."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        _call_lcpencrypt(
            input_file="book.epub",
            contentid="test",
            verbose=True,
        )

        cmd_args = mock_run.call_args.kwargs.get("kwargs_dict") or mock_run.call_args[1].get("kwargs_dict")
        assert cmd_args["verbose"] is True

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_custom_lcpencrypt_binary(self, mock_run, monkeypatch):
        """Test that READIUM_LCPENCRYPT_BIN env var is respected."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")
        monkeypatch.setenv("READIUM_LCPENCRYPT_BIN", "/custom/path/lcpencrypt")

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        _call_lcpencrypt(
            input_file="book.epub",
            contentid="test",
        )

        call_kwargs = mock_run.call_args
        executable_path = call_kwargs.kwargs.get("executable_path") or call_kwargs[1].get("executable_path")
        assert executable_path == "/custom/path/lcpencrypt"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_protected_content_disposition(self, mock_run, monkeypatch):
        """Test that disposition is set to the original filename."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        result = _call_lcpencrypt(
            input_file="catalogs/my-catalog/entry-uuid/my-book.pdf",
            contentid="test",
        )

        assert result["protected_content_disposition"] == "my-book.pdf"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_contentid_fallback_to_generated(self, mock_run, monkeypatch):
        """Test that content_id falls back to 'generated' when not provided."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        result = _call_lcpencrypt(input_file="book.epub")

        assert result["content_id"] == "generated"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_kwargs_prefix_is_single_dash(self, mock_run, monkeypatch):
        """Test that lcpencrypt uses single-dash prefix for arguments."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        _call_lcpencrypt(
            input_file="book.epub",
            contentid="test",
        )

        call_kwargs = mock_run.call_args
        prefix = call_kwargs.kwargs.get("kwargs_key_prefix") or call_kwargs[1].get("kwargs_key_prefix")
        assert prefix == "-"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_temp_defaults_to_tmp(self, mock_run, monkeypatch):
        """Test that temp defaults to /tmp."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        _call_lcpencrypt(
            input_file="book.epub",
            contentid="test",
        )

        cmd_args = mock_run.call_args.kwargs.get("kwargs_dict") or mock_run.call_args[1].get("kwargs_dict")
        assert cmd_args["temp"] == "/tmp"

    @pytest.mark.parametrize("storage", ["catalogs/my-catalog/entry", "s3:eu-west-3:bucket"])
    @pytest.mark.parametrize("url", [None, ""])
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_storage_without_url_is_refused(self, mock_run, monkeypatch, storage, url):
        """lcpencrypt (v1.13.4, v1.14.0) exits with `storage requires url`; the task says so up front."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        kwargs = {"url": url} if url is not None else {}
        result = _call_lcpencrypt(input_file="book.epub", storage=storage, contentid="test-id", **kwargs)

        mock_run.assert_not_called()
        assert result["success"] is False
        assert result["error"] == "Invalid parameters: storage requires url"
        assert result["content_id"] == "test-id"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_absolute_output_not_prepended(self, mock_run, monkeypatch):
        """Test that absolute output paths are not prepended with STORAGE_PATH."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        _call_lcpencrypt(
            input_file="book.epub",
            output="/absolute/path/output",
        )

        cmd_args = mock_run.call_args.kwargs.get("kwargs_dict") or mock_run.call_args[1].get("kwargs_dict")
        assert cmd_args["output"] == "/absolute/path/output"


class TestPdfMetadataPackaging:
    """Tests for injecting title/author into a raw PDF via a Readium package (issue #1)."""

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_pdf_with_title_and_author_is_repackaged(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(b"%PDF-1.4 real-pdf-bytes")

        holder = {}
        mock_run.side_effect = _capture_package(holder)

        result = _call_lcpencrypt(
            input_file="book.pdf",
            contentid="abc-uuid",
            storage="encrypted",
            url="https://cdn.example.com",
            filename="abc-uuid",
            title="The Great Book",
            author="Ada Lovelace",
            temp=str(tmp_path / "tmp"),
        )

        assert result["success"] is True
        # lcpencrypt was handed a Readium package, not the raw PDF
        assert holder["input"].endswith(".rpf")
        assert "publication.pdf" in holder["names"]
        assert holder["manifest"]["metadata"]["title"] == "The Great Book"
        assert holder["manifest"]["metadata"]["author"] == "Ada Lovelace"
        assert holder["manifest"]["metadata"]["conformsTo"] == "https://readium.org/webpub-manifest/profiles/pdf"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_pdf_with_only_title(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        holder = {}
        mock_run.side_effect = _capture_package(holder)

        result = _call_lcpencrypt(
            input_file="book.pdf",
            contentid="id",
            title="Solo Title",
            temp=str(tmp_path / "tmp"),
        )

        assert result["success"] is True
        assert holder["input"].endswith(".rpf")
        assert holder["manifest"]["metadata"]["title"] == "Solo Title"
        assert "author" not in holder["manifest"]["metadata"]

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_pdf_without_metadata_is_not_repackaged(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        _call_lcpencrypt(input_file="book.pdf", contentid="id")

        cmd_args = mock_run.call_args.kwargs.get("kwargs_dict") or mock_run.call_args[1].get("kwargs_dict")
        # No title/author → raw PDF passed straight through
        assert cmd_args["input"] == str(tmp_path / "book.pdf")

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_epub_with_metadata_is_not_repackaged(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        epub = tmp_path / "book.epub"
        epub.write_bytes(b"PK\x03\x04")

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        _call_lcpencrypt(
            input_file="book.epub",
            contentid="id",
            title="Title",
            author="Author",
        )

        cmd_args = mock_run.call_args.kwargs.get("kwargs_dict") or mock_run.call_args[1].get("kwargs_dict")
        # EPUB carries OPF metadata; input must be untouched (never a .rpf)
        assert cmd_args["input"] == str(tmp_path / "book.epub")

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_repackaging_cleans_up_temp_dir(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        temp_root = tmp_path / "tmp"
        holder = {}
        mock_run.side_effect = _capture_package(holder)

        _call_lcpencrypt(
            input_file="book.pdf",
            contentid="id",
            title="Title",
            temp=str(temp_root),
        )

        # The package existed during the run but is removed afterwards
        assert not Path(holder["input"]).exists()
        assert list(temp_root.glob("lcp-rwpp-*")) == []

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_packaging_failure_falls_back_to_raw_pdf(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        # Input PDF does not exist on disk → build_pdf_package raises, fallback kicks in
        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        result = _call_lcpencrypt(
            input_file="missing.pdf",
            contentid="id",
            title="Title",
            author="Author",
            temp=str(tmp_path / "tmp"),
        )

        assert result["success"] is True
        cmd_args = mock_run.call_args.kwargs.get("kwargs_dict") or mock_run.call_args[1].get("kwargs_dict")
        # Falls back to the raw (unresolved) PDF path rather than crashing
        assert cmd_args["input"] == str(tmp_path / "missing.pdf")

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_remote_pdf_is_downloaded_and_packaged(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        source_pdf = tmp_path / "source.pdf"
        source_pdf.write_bytes(b"%PDF-1.4 remote")

        def fake_urlretrieve(url, dest):
            Path(dest).write_bytes(source_pdf.read_bytes())
            return dest, None

        holder = {}
        mock_run.side_effect = _capture_package(holder)

        with patch("evilflowers_lcpencrypt_worker.package.urllib.request.urlretrieve", side_effect=fake_urlretrieve):
            result = _call_lcpencrypt(
                input_file="https://example.com/files/book.pdf?token=xyz",
                contentid="id",
                title="Remote Title",
                author="Remote Author",
                temp=str(tmp_path / "tmp"),
            )

        assert result["success"] is True
        assert holder["input"].endswith(".rpf")
        assert Path(holder["input"]).name == "book.rpf"
        assert holder["manifest"]["metadata"]["title"] == "Remote Title"
        # The download lives in the package's temp dir and goes with it.
        assert list((tmp_path / "tmp").iterdir()) == []


class TestPdfEnrichment:
    """Issue #2: the pre-packaged PDF carries what lcpencrypt adds natively, plus the catalog's metadata."""

    CATALOG_METADATA: Final = {
        "title": "Logika a výpočty",
        "subtitle": "Úvod",
        "authors": ["Ada Lovelace", "Alan Turing"],
        "translators": ["Ján Prekladateľ"],
        "publisher": "STU Press",
        "language": "sk",
        "published": "2021-09-01",
        "description": "Učebnica.",
        "subjects": ["Logic", "Computing"],
        "identifier": "urn:isbn:9788022700000",
    }

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_full_catalog_metadata(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        write_pdf(
            tmp_path / "book.pdf",
            pages=4,
            info={"Title": "ignored", "Author": "ignored"},
            outline=[Bookmark("Intro", 0), Bookmark("Body", 1, [Bookmark("Part", 2)])],
        )
        holder: dict = {}
        mock_run.side_effect = _capture_package(holder)

        result = _call_lcpencrypt(
            input_file="book.pdf",
            contentid="cid",
            filename="cid",
            storage="enc",
            url="https://cdn.example.com",
            temp=str(tmp_path / "tmp"),
            **self.CATALOG_METADATA,
        )

        assert result["success"] is True
        meta = holder["manifest"]["metadata"]
        assert meta == {
            "@type": "http://schema.org/Book",
            "conformsTo": "https://readium.org/webpub-manifest/profiles/pdf",
            "title": "Logika a výpočty",
            "subtitle": "Úvod",
            "author": ["Ada Lovelace", "Alan Turing"],
            "translator": "Ján Prekladateľ",
            "publisher": "STU Press",
            "language": "sk",
            "published": "2021-09-01",
            "description": "Učebnica.",
            "subject": ["Logic", "Computing"],
            "identifier": "urn:isbn:9788022700000",
            "numberOfPages": 4,
        }
        assert holder["manifest"]["resources"] == [{"rel": ["cover"], "href": "cid.jpg", "type": "image/jpeg"}]
        assert [link["href"] for link in holder["manifest"]["toc"]] == [
            "publication.pdf#page=1",
            "publication.pdf#page=2",
        ]
        assert holder["manifest"]["toc"][1]["children"] == [{"href": "publication.pdf#page=3", "title": "Part"}]

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_partial_published_date_is_dropped(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        write_pdf(tmp_path / "book.pdf")
        holder: dict = {}
        mock_run.side_effect = _capture_package(holder)

        _call_lcpencrypt(input_file="book.pdf", contentid="c", title="T", published="2021", temp=str(tmp_path / "t"))

        assert "published" not in holder["manifest"]["metadata"]

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_metadata_without_title_triggers_packaging(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        write_pdf(tmp_path / "my_book.pdf", info={"Title": "Embedded Title"})
        holder: dict = {}
        mock_run.side_effect = _capture_package(holder)

        _call_lcpencrypt(input_file="my_book.pdf", contentid="c", publisher="STU", temp=str(tmp_path / "t"))

        assert holder["manifest"]["metadata"]["title"] == "Embedded Title"
        assert holder["manifest"]["metadata"]["publisher"] == "STU"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_catalog_cover_relative_to_storage_path(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        write_pdf(tmp_path / "book.pdf")
        (tmp_path / "covers").mkdir()
        Image.new("RGB", (600, 900), (0, 128, 0)).save(tmp_path / "covers" / "c.png")
        holder: dict = {}
        mock_run.side_effect = _capture_package(holder)

        _call_lcpencrypt(
            input_file="book.pdf",
            contentid="cid",
            title="T",
            cover="covers/c.png",
            temp=str(tmp_path / "t"),
        )

        with Image.open(io.BytesIO(holder["files"]["cid.jpg"])) as cover:
            # The catalog's green cover (scaled to fit 1200 px), not a render of the PDF's page.
            assert cover.size == (600, 900)
            red, green, blue = cover.getpixel((300, 450))
            assert green > 100 and red < 30 and blue < 30

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_remote_catalog_cover_is_downloaded(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        write_pdf(tmp_path / "book.pdf")
        cover_source = tmp_path / "remote.jpg"
        Image.new("RGB", (300, 300), (0, 0, 255)).save(cover_source)
        seen: list[str] = []

        def fake_urlretrieve(url, dest):
            seen.append(url)
            Path(dest).write_bytes(cover_source.read_bytes())
            return dest, None

        holder: dict = {}
        mock_run.side_effect = _capture_package(holder)

        with patch("evilflowers_lcpencrypt_worker.package.urllib.request.urlretrieve", side_effect=fake_urlretrieve):
            _call_lcpencrypt(
                input_file="book.pdf",
                contentid="cid",
                title="T",
                cover="https://elvira.example/covers/1.jpg",
                temp=str(tmp_path / "t"),
            )

        assert seen == ["https://elvira.example/covers/1.jpg"]
        assert "cid.jpg" in holder["names"]
        assert list((tmp_path / "t").iterdir()) == []

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_unreachable_catalog_cover_falls_back_to_first_page(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        write_pdf(tmp_path / "book.pdf")
        holder: dict = {}
        mock_run.side_effect = _capture_package(holder)

        with patch(
            "evilflowers_lcpencrypt_worker.package.urllib.request.urlretrieve", side_effect=OSError("unreachable")
        ):
            result = _call_lcpencrypt(
                input_file="book.pdf",
                contentid="cid",
                title="T",
                cover="https://elvira.example/covers/1.jpg",
                temp=str(tmp_path / "t"),
            )

        assert result["success"] is True
        assert "cid.jpg" in holder["names"]


REPORT_URL: Final = "http://lcp:secret@catalog:8000/readium/v1/hooks/encryption/result"


def _catalog_task(tmp_path, **extra):
    """A catalog-style PDF task against STORAGE_PATH=tmp_path, writing `enc/cid.lcpdf` on success."""
    write_pdf(tmp_path / "book.pdf", pages=3, outline=[Bookmark("One", 0), Bookmark("Two", 2)])
    params = {
        "input_file": "book.pdf",
        "contentid": "cid",
        "filename": "cid",
        "storage": "enc",
        "url": "https://elvira.example/content",
        "title": "T",
        "authors": ["A", "B"],
        "temp": str(tmp_path / "tmp"),
    }
    return params | extra


def _writes_package(tmp_path, stdout=""):
    def run(*args, **kwargs):
        (tmp_path / "enc").mkdir(exist_ok=True)
        (tmp_path / "enc" / "cid.lcpdf").write_bytes(b"PK-encrypted")
        return ExecutableResult(returncode=0, stdout=stdout, stderr="")

    return run


class TestOutcomeReport:
    """Issue #3: every outcome reaches the catalog's `report` URL."""

    @patch("evilflowers_lcpencrypt_worker.send_report")
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_success_is_reported_with_the_package_summary(self, mock_run, mock_send, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        mock_run.side_effect = _writes_package(tmp_path)

        result = _call_lcpencrypt(**_catalog_task(tmp_path, report=REPORT_URL))

        assert result["success"] is True
        (url, payload), _ = mock_send.call_args
        assert url == REPORT_URL
        assert payload["contentid"] == "cid"
        assert payload["success"] is True
        assert payload["error"] == ""
        assert payload["report"] == {
            "cover": "rendered",
            "pages": 3,
            "toc_entries": 2,
            "warnings": [],
            "manifest": "enc/cid.manifest.json",
            "protected_content_location": "https://elvira.example/content/cid.lcpdf",
            "protected_content_type": "application/pdf+lcp",
            "protected_content_length": len(b"PK-encrypted"),
            "protected_content_sha256": hashlib.sha256(b"PK-encrypted").hexdigest(),
        }
        # The report URL is the worker's own: lcpencrypt never sees it.
        assert "report" not in mock_run.call_args.kwargs["kwargs_dict"]

    @patch("evilflowers_lcpencrypt_worker.send_report")
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_lcpencrypt_failure_is_reported(self, mock_run, mock_send, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        mock_run.side_effect = ExecutableException("lcpencrypt", 1, "Error processing a publication : boom\n", "")

        result = _call_lcpencrypt(**_catalog_task(tmp_path, report=REPORT_URL))

        assert result["success"] is False
        payload = mock_send.call_args.args[1]
        assert payload["success"] is False
        assert payload["error"] == result["error"]
        assert "boom" in payload["error"]
        # The package was built before lcpencrypt failed: the catalog sees what it carried.
        assert payload["report"]["cover"] == "rendered"
        assert "protected_content_location" not in payload["report"]

    @patch("evilflowers_lcpencrypt_worker.send_report")
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_refused_parameters_are_reported(self, mock_run, mock_send, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        _call_lcpencrypt(input_file="book.pdf", contentid="cid", storage="enc", report=REPORT_URL)

        mock_run.assert_not_called()
        payload = mock_send.call_args.args[1]
        assert payload["success"] is False
        assert payload["error"] == "Invalid parameters: storage requires url"

    @patch("evilflowers_lcpencrypt_worker.send_report")
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_worker_bug_is_reported_and_raised(self, mock_run, mock_send, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")
        mock_run.side_effect = RuntimeError("unexpected")

        with pytest.raises(RuntimeError, match="unexpected"):
            _call_lcpencrypt(input_file="book.epub", contentid="cid", report=REPORT_URL)

        payload = mock_send.call_args.args[1]
        assert payload["success"] is False
        assert "unexpected" in payload["error"]

    @patch("evilflowers_lcpencrypt_worker.send_report")
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_no_report_url_no_request(self, mock_run, mock_send, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        mock_run.side_effect = _writes_package(tmp_path)

        assert _call_lcpencrypt(**_catalog_task(tmp_path))["success"] is True

        mock_send.assert_not_called()

    @patch("evilflowers_lcpencrypt_worker.send_report", return_value=False)
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_failed_report_leaves_the_result_alone(self, mock_run, mock_send, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        mock_run.side_effect = _writes_package(tmp_path)

        assert _call_lcpencrypt(**_catalog_task(tmp_path, report=REPORT_URL))["success"] is True


class TestCmsNotificationWithdrawal:
    """lcpencrypt v1.14 exits 0 after a failed `-notify`, having withdrawn the content again."""

    @patch("evilflowers_lcpencrypt_worker.send_report")
    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_withdrawn_publication_is_a_failure(self, mock_run, mock_send, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        mock_run.side_effect = _writes_package(
            tmp_path, stdout="Error notifying the CMS: the server returned an error 401\n"
        )

        result = _call_lcpencrypt(**_catalog_task(tmp_path, report=REPORT_URL))

        assert result["success"] is False
        assert result["error"] == (
            "Encryption withdrawn from the License Server: Error notifying the CMS: the server returned an error 401"
        )
        assert mock_send.call_args.args[1]["success"] is False
        # Nothing to inspect for a publication that is not lendable.
        assert not (tmp_path / "enc" / "cid.manifest.json").exists()


class TestManifestSidecar:
    """Issue #4: the package's manifest next to the encrypted file."""

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_sidecar_is_the_packaged_manifest(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        holder: dict = {}
        capture = _capture_package(holder)

        def run(*args, **kwargs):
            capture(*args, **kwargs)
            return _writes_package(tmp_path)(*args, **kwargs)

        mock_run.side_effect = run

        _call_lcpencrypt(**_catalog_task(tmp_path))

        sidecar = json.loads((tmp_path / "enc" / "cid.manifest.json").read_text())
        assert sidecar == holder["manifest"]
        assert sidecar["metadata"]["author"] == ["A", "B"]

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_sidecar_can_be_turned_off(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        monkeypatch.setenv("MANIFEST_SIDECAR", "false")
        mock_run.side_effect = _writes_package(tmp_path)

        _call_lcpencrypt(**_catalog_task(tmp_path))

        assert sorted(path.name for path in (tmp_path / "enc").iterdir()) == ["cid.lcpdf"]

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_no_sidecar_without_a_built_package(self, mock_run, tmp_path, monkeypatch):
        """EPUB (and PDF without metadata): lcpencrypt builds the package, there is no manifest of ours."""
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        (tmp_path / "enc").mkdir()
        (tmp_path / "enc" / "cid.epub").write_bytes(b"PK")
        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        _call_lcpencrypt(input_file="book.epub", contentid="cid", filename="cid", storage="enc", url="https://x")

        assert sorted(path.name for path in (tmp_path / "enc").iterdir()) == ["cid.epub"]

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_no_sidecar_for_s3(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        write_pdf(tmp_path / "book.pdf")
        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        result = _call_lcpencrypt(
            input_file="book.pdf",
            contentid="cid",
            storage="s3:eu-west-3:bucket",
            url="https://bucket",
            title="T",
            temp=str(tmp_path / "tmp"),
        )

        assert result["success"] is True
        assert not list(tmp_path.glob("**/*.manifest.json"))


class TestPackagePreview:
    """Issue #4: what the reader will show, without encrypting anything."""

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_preview_returns_and_stores_manifest_and_cover(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        params = _catalog_task(tmp_path, report=REPORT_URL, notify="http://u:p@catalog/hook")

        with patch("evilflowers_lcpencrypt_worker.send_report") as mock_send:
            result = package_preview.run(**params)

        mock_run.assert_not_called()
        mock_send.assert_not_called()
        assert result["success"] is True
        assert result["error"] == ""
        assert result["manifest"]["metadata"]["title"] == "T"
        assert result["manifest"]["metadata"]["numberOfPages"] == 3
        assert result["manifest"]["resources"] == [{"rel": ["cover"], "href": "cid.jpg", "type": "image/jpeg"}]
        assert (result["cover"], result["pages"], result["toc_entries"]) == ("rendered", 3, 2)
        cover = base64.b64decode(result["cover_jpeg_base64"])
        assert cover.startswith(b"\xff\xd8")  # JPEG
        assert result["manifest_path"] == "enc/cid.preview.manifest.json"
        assert result["cover_path"] == "enc/cid.preview.jpg"
        assert json.loads((tmp_path / "enc" / "cid.preview.manifest.json").read_text()) == result["manifest"]
        assert (tmp_path / "enc" / "cid.preview.jpg").read_bytes() == cover
        # Neither the encrypted package nor the real sidecar: nothing was encrypted.
        assert sorted(path.name for path in (tmp_path / "enc").iterdir()) == [
            "cid.preview.jpg",
            "cid.preview.manifest.json",
        ]
        assert list((tmp_path / "tmp").iterdir()) == []

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_preview_matches_what_the_task_encrypts(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        holder: dict = {}
        mock_run.side_effect = _capture_package(holder)
        params = _catalog_task(tmp_path)
        del params["storage"], params["url"]

        preview = package_preview.run(**params)
        _call_lcpencrypt(**params)

        assert preview["manifest"] == holder["manifest"]
        assert base64.b64decode(preview["cover_jpeg_base64"]) == holder["files"]["cid.jpg"]
        assert preview["manifest_path"] == ""

    def test_preview_without_metadata_warns(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
        write_pdf(tmp_path / "book.pdf")

        result = package_preview.run(input_file="book.pdf", contentid="cid", temp=str(tmp_path / "tmp"))

        assert result["success"] is True
        assert any("no publication metadata" in warning for warning in result["warnings"])

    def test_preview_of_epub_is_refused(self):
        result = package_preview.run(input_file="book.epub", title="T")

        assert result["success"] is False
        assert "PDF input only" in result["error"]

    def test_preview_of_a_missing_file_fails_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        result = package_preview.run(input_file="missing.pdf", title="T", temp=str(tmp_path / "tmp"))

        assert result["success"] is False
        assert result["error"].startswith("Package preview failed:")
        assert list((tmp_path / "tmp").iterdir()) == []
