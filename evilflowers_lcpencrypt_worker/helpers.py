import logging
import subprocess
from typing import Dict, Tuple, Optional, List


class ExecutableException(Exception):
    def __init__(self, executable_path, returncode, stdout, stderr, command: Optional[str] = None):
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._command = command

        super().__init__(f"{executable_path} exited with {self._returncode}")


def run_executable(
    executable_path: str,
    args_list: List[str] = None,
    kwargs_dict: Dict[str, str] = None,
    kwargs_key_prefix: str = "--",
) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    # Convert the dictionary of arguments to a list suitable for subprocess
    kwargs_list = []
    for k, v in kwargs_dict.items():
        if v is not None:
            kwargs_list.append(f"{kwargs_key_prefix}{k}={v}")

    # Combine the executable path and arguments
    command = [executable_path] + (args_list or []) + (kwargs_list or {})

    # Run the command and capture the output
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    # Print the captured error output (if any)
    if result.stderr:
        logging.error(f"Command failed with stderr: {result.stderr}")

    # Check if the return code is zero (which usually indicates success)
    if result.returncode != 0:
        raise ExecutableException(executable_path, result.returncode, result.stdout, result.stderr)

    return result.returncode, result.stdout, result.stderr
