"""The command line's own exit statuses, and how a failure is reported.

125, 126, 127 and 130 are the CLI's: `run` passes the remote command's
status through, so a 1 or 2 of its own would read as the remote program's.
"""

from __future__ import annotations

import subprocess
import typing

from ..exceptions import BundleError, OperationNotStartedError

#: The first `isinstance` match wins, so both OSError subclasses come before
#: OSError itself.
ERRORS: typing.Dict[type, int] = {
    subprocess.TimeoutExpired: 125,
    # A step `deploy` ran on the target failed; its stderr is in the report.
    subprocess.CalledProcessError: 125,
    BundleError: 125,
    PermissionError: 126,
    FileNotFoundError: 127,
    # Raised only before anything was sent: the host was never reached.
    OperationNotStartedError: 125,
    ConnectionError: 125,
    OSError: 125,
    ValueError: 125,
    NotImplementedError: 125,
}

EXIT_CODES = {
    125: "connection, timeout, usage, or an unsupported operation",
    126: "permission denied",
    127: "not found",
    130: "interrupted",
}

_PREFIXES = (
    (subprocess.TimeoutExpired, "timed out: "),
    (PermissionError, "permission denied: "),
    (FileNotFoundError, "not found: "),
)


def status_of(error: BaseException) -> typing.Optional[int]:
    """The exit status for `error`, or `None` when the CLI does not map it."""
    return next(
        (code for kind, code in ERRORS.items() if isinstance(error, kind)), None
    )


def describe(message: str, error: BaseException) -> str:
    """`message` with the failure's kind in front and, for a failed step,
    the last line it wrote to stderr after it."""
    prefix = next((text for kind, text in _PREFIXES if isinstance(error, kind)), "")
    text = f"{prefix}{message}"
    if isinstance(error, subprocess.CalledProcessError):
        stderr = error.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "backslashreplace")
        last = [line for line in (stderr or "").splitlines() if line.strip()][-1:]
        if last:
            text = f"{text}: {last[0].strip()}"
    return text
