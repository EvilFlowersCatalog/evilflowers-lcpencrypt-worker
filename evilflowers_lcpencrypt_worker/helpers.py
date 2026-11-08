"""Helpers for executing external binaries."""

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from evilflowers_lcpencrypt_worker.log_format import redact

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

type CliValue = str | int | float | bool | None
"""A keyword argument value: `True` becomes a bare flag, `False`/`None` are dropped."""


class ExecutableException(Exception):
    """An executable exited with a non-zero return code.

    Attributes:
        executable_path: Path to the executable that failed.
        returncode: Exit code returned by the executable.
        stdout: Standard output from the executable.
        stderr: Standard error from the executable.
        command: Full command that was executed (optional).
    """

    executable_path: str
    returncode: int
    stdout: str
    stderr: str
    command: str | None

    def __init__(
        self,
        executable_path: str,
        returncode: int,
        stdout: str,
        stderr: str,
        command: str | None = None,
    ) -> None:
        self.executable_path = executable_path
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.command = command
        super().__init__(f"{executable_path} exited with return code {returncode}")


@dataclass(frozen=True, slots=True)
class ExecutableResult:
    """Result of a successful run of an external binary."""

    returncode: int
    stdout: str
    stderr: str


def build_command(
    executable_path: str,
    args_list: Sequence[str] | None = None,
    kwargs_dict: Mapping[str, CliValue] | None = None,
    kwargs_key_prefix: str = "--",
) -> list[str]:
    """Build the argv: executable, positional arguments, then `<prefix>key=value` options."""
    options: list[str] = []
    for key, value in (kwargs_dict or {}).items():
        match value:
            case None | False:
                continue
            case True:
                options.append(f"{kwargs_key_prefix}{key}")
            case _:
                options.append(f"{kwargs_key_prefix}{key}={value}")
    return [executable_path, *(args_list or []), *options]


def run_executable(
    executable_path: str,
    args_list: Sequence[str] | None = None,
    kwargs_dict: Mapping[str, CliValue] | None = None,
    kwargs_key_prefix: str = "--",
) -> ExecutableResult:
    """Execute an external binary with the given arguments.

    Args:
        executable_path: Path to the executable to run.
        args_list: Positional arguments to pass to the executable.
        kwargs_dict: Keyword arguments. `True` becomes a bare flag, `None`/`False` are dropped,
            anything else is passed as `<prefix>key=value`.
        kwargs_key_prefix: Prefix for keyword argument keys (default: `--`).

    Returns:
        ExecutableResult with the return code, stdout and stderr.

    Raises:
        ExecutableException: If the executable returns a non-zero exit code.

    Example:
        >>> result = run_executable(
        ...     "/usr/bin/lcpencrypt",
        ...     kwargs_dict={"input": "file.epub", "verbose": True},
        ...     kwargs_key_prefix="-",
        ... )
    """
    kwargs_dict = kwargs_dict or {}

    # Filesystem storage only: lcpencrypt does not create the target directory itself.
    match kwargs_dict.get("storage"):
        case str(storage_path) if storage_path and not storage_path.startswith("s3:"):
            os.makedirs(storage_path, exist_ok=True)
            logger.debug("Ensured storage directory exists: %s", storage_path)
        case _:
            pass

    command = build_command(executable_path, args_list, kwargs_dict, kwargs_key_prefix)
    printable = shlex.join(command)
    logger.info("Executing command: %s", _redact(printable))

    result = subprocess.run(  # noqa: UP022 - explicit pipes; tests pin these kwargs
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    if result.stdout:
        logger.debug("Command stdout: %s", result.stdout)

    if result.returncode != 0:
        logger.error(
            "Command failed with return code %s: %s",
            result.returncode,
            _redact(printable),
            extra={"stderr": result.stderr},
        )
        raise ExecutableException(
            executable_path=executable_path,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            command=" ".join(command),
        )

    # lcpencrypt (Go `log`) writes its normal progress to stderr: not a warning on success.
    if result.stderr:
        logger.debug("Command stderr: %s", result.stderr)

    return ExecutableResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)


def _redact(command: str) -> str:
    """Hide `user:password@` in URLs (`-lcpsv`, `-notify`) even under a formatter that does not."""
    return redact(command)


__all__ = ["CliValue", "ExecutableException", "ExecutableResult", "build_command", "run_executable"]
