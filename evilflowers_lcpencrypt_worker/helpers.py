"""Helper functions for executing external binaries."""

import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


class ExecutableException(Exception):
    """Exception raised when an executable exits with a non-zero return code.

    Attributes:
        executable_path: Path to the executable that failed.
        returncode: Exit code returned by the executable.
        stdout: Standard output from the executable.
        stderr: Standard error from the executable.
        command: Full command that was executed (optional).
    """

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


class ExecutableResult:
    """Result from executing an external binary.

    Attributes:
        returncode: Exit code returned by the executable.
        stdout: Standard output from the executable.
        stderr: Standard error from the executable.
    """

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run_executable(
    executable_path: str,
    args_list: list[str] | None = None,
    kwargs_dict: dict[str, Any] | None = None,
    kwargs_key_prefix: str = "--",
) -> ExecutableResult:
    """Execute an external binary with the given arguments.

    Args:
        executable_path: Path to the executable to run.
        args_list: Positional arguments to pass to the executable.
        kwargs_dict: Keyword arguments to pass to the executable. Boolean values
                     will be converted to flags (just the key), other values will
                     be passed as key=value pairs.
        kwargs_key_prefix: Prefix for keyword argument keys (default: '--').

    Returns:
        ExecutableResult containing the return code, stdout, and stderr.

    Raises:
        ExecutableException: If the executable returns a non-zero exit code.

    Example:
        >>> result = run_executable(
        ...     "/usr/bin/lcpencrypt",
        ...     kwargs_dict={"input": "file.epub", "verbose": True},
        ...     kwargs_key_prefix="-"
        ... )
        >>> print(result.stdout)
    """
    if kwargs_dict is None:
        kwargs_dict = {}

    # Create storage directory if needed (filesystem storage only)
    if "storage" in kwargs_dict:
        storage_path = kwargs_dict["storage"]
        if storage_path and not storage_path.startswith("s3:"):
            os.makedirs(storage_path, exist_ok=True)
            logger.debug(f"Ensured storage directory exists: {storage_path}")

    # Convert keyword arguments to command-line format
    kwargs_list = []
    for key, value in kwargs_dict.items():
        if value is None:
            continue

        if isinstance(value, bool):
            if value:
                # Boolean true becomes a flag
                kwargs_list.append(f"{kwargs_key_prefix}{key}")
        else:
            # Other values become key=value pairs
            kwargs_list.append(f"{kwargs_key_prefix}{key}={value}")

    # Build complete command
    command = [executable_path] + (args_list or []) + kwargs_list

    logger.info(f"Executing command: {' '.join(command)}")

    # Execute the command
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    # Log output
    if result.stdout:
        logger.debug(f"Command stdout: {result.stdout}")
    if result.stderr:
        logger.warning(f"Command stderr: {result.stderr}")

    # Check for errors
    if result.returncode != 0:
        logger.error(f"Command failed with return code {result.returncode}: {' '.join(command)}")
        raise ExecutableException(
            executable_path=executable_path,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            command=" ".join(command),
        )

    return ExecutableResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
