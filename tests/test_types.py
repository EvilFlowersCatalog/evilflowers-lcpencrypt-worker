"""Tests for the types module (TypedDict definitions)."""

from typing import get_type_hints

from evilflowers_lcpencrypt_worker.types import (
    LCPEncryptParams,
    LCPEncryptResult,
    StorageMode,
)


class TestLCPEncryptParams:
    def test_can_construct_minimal(self):
        params: LCPEncryptParams = {"input_file": "book.epub"}
        assert params["input_file"] == "book.epub"

    def test_can_construct_full_recommended_mode(self):
        params: LCPEncryptParams = {
            "input_file": "book.epub",
            "storage": "s3:eu-west-3:bucket",
            "url": "https://bucket.s3.eu-west-3.amazonaws.com",
            "contentid": "book-123",
            "filename": "book-123.epub",
            "temp": "/tmp",
            "lcpsv": "http://lcpsv:8989",
            "notify": "http://cms:8000/hooks",
            "verbose": True,
        }
        assert params["storage"] == "s3:eu-west-3:bucket"
        assert params["verbose"] is True

    def test_can_construct_legacy_mode(self):
        params: LCPEncryptParams = {
            "input_file": "book.epub",
            "output": "/tmp/encrypted",
            "login": "admin",
            "password": "secret",
        }
        assert params["login"] == "admin"

    def test_has_expected_fields(self):
        hints = get_type_hints(LCPEncryptParams)
        expected_fields = {
            "input_file",
            "storage",
            "url",
            "contentid",
            "filename",
            "temp",
            "lcpsv",
            "notify",
            "verbose",
            "output",
            "login",
            "password",
        }
        assert set(hints.keys()) == expected_fields

    def test_total_false_allows_partial(self):
        # TypedDict with total=False means all fields are optional
        params: LCPEncryptParams = {}
        assert isinstance(params, dict)


class TestStorageMode:
    def test_valid_values(self):
        mode_not_stored: StorageMode = 0
        mode_s3: StorageMode = 1
        mode_filesystem: StorageMode = 2
        assert mode_not_stored == 0
        assert mode_s3 == 1
        assert mode_filesystem == 2


class TestLCPEncryptResult:
    def test_can_construct_success_result(self):
        result: LCPEncryptResult = {
            "content_id": "book-123",
            "content_encryption_key": "abc123key",
            "storage_mode": 2,
            "protected_content_location": "/mnt/data/encrypted/book.epub",
            "protected_content_disposition": "book.epub",
            "protected_content_type": "application/epub+zip",
            "protected_content_length": 1024,
            "protected_content_sha256": "sha256hash",
            "success": True,
            "error": "",
        }
        assert result["success"] is True
        assert result["content_id"] == "book-123"

    def test_can_construct_error_result(self):
        result: LCPEncryptResult = {
            "content_id": "unknown",
            "content_encryption_key": "",
            "storage_mode": 0,
            "protected_content_location": "",
            "protected_content_disposition": "book.epub",
            "protected_content_type": "",
            "protected_content_length": 0,
            "protected_content_sha256": "",
            "success": False,
            "error": "Encryption failed: binary not found",
        }
        assert result["success"] is False
        assert "Encryption failed" in result["error"]

    def test_has_expected_fields(self):
        hints = get_type_hints(LCPEncryptResult)
        expected_fields = {
            "content_id",
            "content_encryption_key",
            "storage_mode",
            "protected_content_location",
            "protected_content_disposition",
            "protected_content_type",
            "protected_content_length",
            "protected_content_sha256",
            "success",
            "error",
        }
        assert set(hints.keys()) == expected_fields
