"""Tests for the helpers module (run_executable, ExecutableException, ExecutableResult)."""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from evilflowers_lcpencrypt_worker.helpers import (
    ExecutableException,
    ExecutableResult,
    run_executable,
)


class TestExecutableException:
    def test_attributes(self):
        exc = ExecutableException(
            executable_path="/usr/bin/lcpencrypt",
            returncode=1,
            stdout="some output",
            stderr="some error",
            command="/usr/bin/lcpencrypt -input=file.epub",
        )
        assert exc.executable_path == "/usr/bin/lcpencrypt"
        assert exc.returncode == 1
        assert exc.stdout == "some output"
        assert exc.stderr == "some error"
        assert exc.command == "/usr/bin/lcpencrypt -input=file.epub"

    def test_str_representation(self):
        exc = ExecutableException(
            executable_path="/usr/bin/lcpencrypt",
            returncode=127,
            stdout="",
            stderr="command not found",
        )
        assert "/usr/bin/lcpencrypt" in str(exc)
        assert "127" in str(exc)

    def test_command_is_optional(self):
        exc = ExecutableException(
            executable_path="binary",
            returncode=1,
            stdout="",
            stderr="",
        )
        assert exc.command is None

    def test_is_exception(self):
        exc = ExecutableException("bin", 1, "", "")
        assert isinstance(exc, Exception)


class TestExecutableResult:
    def test_attributes(self):
        result = ExecutableResult(returncode=0, stdout="output", stderr="warning")
        assert result.returncode == 0
        assert result.stdout == "output"
        assert result.stderr == "warning"


class TestRunExecutable:
    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_simple_execution(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="success", stderr="")

        result = run_executable("/usr/bin/echo")

        mock_run.assert_called_once()
        assert result.returncode == 0
        assert result.stdout == "success"

    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_with_args_list(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_executable("/usr/bin/echo", args_list=["hello", "world"])

        cmd = mock_run.call_args[0][0]
        assert cmd == ["/usr/bin/echo", "hello", "world"]

    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_kwargs_string_values(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_executable(
            "/usr/bin/lcpencrypt",
            kwargs_dict={"input": "file.epub", "contentid": "abc-123"},
            kwargs_key_prefix="-",
        )

        cmd = mock_run.call_args[0][0]
        assert "-input=file.epub" in cmd
        assert "-contentid=abc-123" in cmd

    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_kwargs_boolean_true_becomes_flag(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_executable(
            "/usr/bin/lcpencrypt",
            kwargs_dict={"verbose": True},
            kwargs_key_prefix="-",
        )

        cmd = mock_run.call_args[0][0]
        assert "-verbose" in cmd

    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_kwargs_boolean_false_skipped(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_executable(
            "/usr/bin/lcpencrypt",
            kwargs_dict={"verbose": False},
            kwargs_key_prefix="-",
        )

        cmd = mock_run.call_args[0][0]
        assert "-verbose" not in cmd

    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_kwargs_none_skipped(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_executable(
            "/usr/bin/lcpencrypt",
            kwargs_dict={"input": "file.epub", "optional": None},
            kwargs_key_prefix="--",
        )

        cmd = mock_run.call_args[0][0]
        assert "--input=file.epub" in cmd
        assert "--optional" not in " ".join(cmd)

    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_default_kwargs_key_prefix(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_executable("/bin/cmd", kwargs_dict={"key": "val"})

        cmd = mock_run.call_args[0][0]
        assert "--key=val" in cmd

    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_nonzero_exit_raises_exception(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="out", stderr="err")

        with pytest.raises(ExecutableException) as exc_info:
            run_executable("/usr/bin/lcpencrypt")

        assert exc_info.value.returncode == 1
        assert exc_info.value.stdout == "out"
        assert exc_info.value.stderr == "err"

    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_exception_includes_command(self, mock_run):
        mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="fail")

        with pytest.raises(ExecutableException) as exc_info:
            run_executable("/bin/cmd", args_list=["arg1"])

        assert "/bin/cmd arg1" in exc_info.value.command

    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_filesystem_storage_creates_directory(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        storage_dir = str(tmp_path / "new_storage_dir")

        run_executable(
            "/bin/cmd",
            kwargs_dict={"storage": storage_dir, "input": "file.epub"},
        )

        assert os.path.isdir(storage_dir)

    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_s3_storage_does_not_create_directory(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_executable(
            "/bin/cmd",
            kwargs_dict={"storage": "s3:eu-west-3:my-bucket"},
        )

        # Should not try to create a directory for S3
        assert not os.path.exists("s3:eu-west-3:my-bucket")

    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_subprocess_called_with_correct_params(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_executable("/bin/echo")

        mock_run.assert_called_once_with(
            ["/bin/echo"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    @patch("evilflowers_lcpencrypt_worker.helpers.subprocess.run")
    def test_args_and_kwargs_combined(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_executable(
            "/bin/cmd",
            args_list=["positional"],
            kwargs_dict={"flag": True, "key": "val"},
            kwargs_key_prefix="--",
        )

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/bin/cmd"
        assert cmd[1] == "positional"
        assert "--flag" in cmd
        assert "--key=val" in cmd