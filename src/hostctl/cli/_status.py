"""The command line's own exit statuses, and how a failure is reported.

125, 126, 127 and 130 are the CLI's: `run` passes the remote command's
status through, so a 1 or 2 of its own would read as the remote program's.
"""

from __future__ import annotations

import subprocess
import typing

from ..exceptions import BundleError, MissingExtraError, OperationNotStartedError

#: The first `isinstance` match wins, so both OSError subclasses come before
#: OSError itself.
ERRORS: typing.Dict[type, int] = {
    subprocess.TimeoutExpired: 125,
    # A step `deploy` ran on the target failed; its stderr is in the report.
    subprocess.CalledProcessError: 125,
    BundleError: 125,
    # Only hostctl's own: an ImportError raised inside an installed
    # dependency is a defect, and keeps its traceback.
    MissingExtraError: 125,
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
    125: "connection, timeout, usage, a missing extra, or an unsupported operation",
    126: "permission denied",
    127: "not found",
    130: "interrupted",
}

_PREFIXES = (
    (subprocess.TimeoutExpired, "timed out: "),
    (PermissionError, "permission denied: "),
    (FileNotFoundError, "not found: "),
)


def worst(codes: typing.Sequence[int]) -> int:
    """The status of a run over several hosts: the worst of theirs.

    130 (interrupted) ranks first, then the CLI's own 125-127 (the command
    never ran on that host), then the largest remote status by magnitude,
    then 0. An empty list is 0.
    """

    def rank(code: int) -> typing.Tuple[int, int]:
        if code == 130:
            return (3, 0)
        if code in (125, 126, 127):
            return (2, code)
        return (1 if code else 0, abs(code))

    return max(codes, key=rank, default=0)


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
