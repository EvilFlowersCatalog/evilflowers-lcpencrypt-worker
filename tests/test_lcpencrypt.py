"""Tests for the lcpencrypt Celery task and helper functions."""

import hashlib
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from evilflowers_lcpencrypt_worker import _determine_storage_mode, _get_file_info, lcpencrypt
from evilflowers_lcpencrypt_worker.helpers import ExecutableException, ExecutableResult


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

        mime_type, size, sha256 = _get_file_info(str(audiobook))

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


def _call_lcpencrypt(**kwargs):
    """Call the lcpencrypt task directly (Celery handles self binding)."""
    return lcpencrypt.run(**kwargs)


class TestLcpencryptTask:
    """Tests for the lcpencrypt Celery task.

    All tests mock the subprocess execution and file system interactions
    to test the task logic independently of the lcpencrypt binary.
    """

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_catalog_style_filesystem_storage(self, mock_run, tmp_path, monkeypatch):
        """Test the exact parameters the EvilFlowersCatalog sends.

        The catalog sends: input_file, contentid, storage, filename, lcpsv, notify
        It does NOT send: url
        """
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        # Simulate catalog file structure
        catalog_dir = tmp_path / "catalogs" / "my-catalog" / "entry-uuid"
        catalog_dir.mkdir(parents=True)
        encrypted_dir = catalog_dir / "encrypted"
        encrypted_dir.mkdir()

        input_file = catalog_dir / "book.pdf"
        input_file.write_bytes(b"%PDF-1.4 content")

        # Simulate encrypted output (created by lcpencrypt binary)
        output_file = encrypted_dir / "abc-123-uuid.lcp.pdf"
        output_file.write_bytes(b"%PDF-encrypted-content")

        mock_run.return_value = ExecutableResult(
            returncode=0,
            stdout="Content encryption key: deadbeef1234\n",
            stderr="",
        )

        result = _call_lcpencrypt(
            input_file="catalogs/my-catalog/entry-uuid/book.pdf",
            contentid="abc-123-uuid",
            storage="catalogs/my-catalog/entry-uuid",
            filename="encrypted/abc-123-uuid.lcp.pdf",
            lcpsv="http://readium:8989",
            notify="http://django:8000/api/readium/hooks/encryption",
        )

        assert result["success"] is True
        assert result["content_id"] == "abc-123-uuid"
        assert result["storage_mode"] == 2  # filesystem

        # Verify storage was passed to the binary
        cmd_args = mock_run.call_args.kwargs.get("kwargs_dict") or mock_run.call_args[1].get("kwargs_dict")
        assert cmd_args["storage"] == str(catalog_dir)
        assert cmd_args["filename"] == "encrypted/abc-123-uuid.lcp.pdf"
        assert cmd_args["lcpsv"] == "http://readium:8989"
        assert cmd_args["notify"] == "http://django:8000/api/readium/hooks/encryption"

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

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_output_location_filesystem_no_url(self, mock_run, tmp_path, monkeypatch):
        """Test that output_location is a local path when url is not provided."""
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        result = _call_lcpencrypt(
            input_file="book.epub",
            storage="catalogs/my-catalog/entry-uuid",
            contentid="abc-uuid",
            filename="encrypted/abc-uuid.lcp.pdf",
        )

        expected_path = str(tmp_path / "catalogs" / "my-catalog" / "entry-uuid" / "encrypted" / "abc-uuid.lcp.pdf")
        assert result["protected_content_location"] == expected_path

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_s3_storage_without_url(self, mock_run, monkeypatch):
        """Test S3 storage without URL returns just the filename."""
        monkeypatch.setenv("STORAGE_PATH", "/mnt/data")

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        result = _call_lcpencrypt(
            input_file="book.epub",
            storage="s3:eu-west-3:bucket",
            contentid="test",
            filename="test.epub",
        )

        assert result["protected_content_location"] == "test.epub"

    @patch("evilflowers_lcpencrypt_worker.run_executable")
    def test_storage_without_url_passes_storage_to_binary(self, mock_run, tmp_path, monkeypatch):
        """Verify that storage is passed to the binary even without url."""
        monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

        mock_run.return_value = ExecutableResult(returncode=0, stdout="", stderr="")

        _call_lcpencrypt(
            input_file="book.epub",
            storage="catalogs/my-catalog/entry",
            filename="encrypted/out.lcp.pdf",
            contentid="test-id",
        )

        cmd_args = mock_run.call_args.kwargs.get("kwargs_dict") or mock_run.call_args[1].get("kwargs_dict")
        assert "storage" in cmd_args
        assert cmd_args["storage"] == str(tmp_path / "catalogs" / "my-catalog" / "entry")

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