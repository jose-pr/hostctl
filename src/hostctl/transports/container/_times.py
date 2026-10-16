"""Setting a container file's times with an exec: the archive API has no call.

Re-putting the member through `put_archive` with a new tar mtime would send
the whole content again to change one header field, so the time is set by a
program in the container instead: `touch` on Linux, Windows PowerShell on a
Windows container. A container with neither cannot set a time.
"""

from __future__ import annotations

import errno
import subprocess
import typing

from ...paths._utime import Touch

#: `touch -c` on this path changes nothing: no container has the directory.
_PROBE_PATH = "/.hostctl-utime-probe/absent"

#: One PowerShell script as one argv element, well inside CreateProcess's
#: 32767-character command line.
_WINDOWS_SCRIPT_BUDGET = 16_000

#: How an engine reports an exec of a program the image does not have.
_MISSING_PROGRAM = ("executable file not found", "no such file or directory")

_UNPROBED = object()


class ExecTimes:
    """Sets times through `exec_run`, probed once per container backend.

    Linux runs `touch` as uid 0, the privilege `put_archive` writes with, so
    a file the archive API created is one the exec may change.
    """

    def __init__(self, engine: typing.Callable[[], object], *, windows: bool):
        self._engine = engine
        self._windows = windows
        self._setter: object = _UNPROBED

    def _exec(self, argv: typing.Sequence[str]) -> typing.Tuple[int, bytes, bytes]:
        container = self._engine()
        exec_run = getattr(container, "exec_run", None)
        if exec_run is None:
            raise NotImplementedError("this container client has no exec")
        options: typing.Dict[str, object] = {
            "stdout": True,
            "stderr": True,
            "demux": True,
        }
        if not self._windows:
            # The archive API resolves a relative path against `/`.
            options.update(user="0", workdir="/")
        result = exec_run(list(argv), **options)
        code = getattr(result, "exit_code", None)
        output = getattr(result, "output", None)
        out, err = output if isinstance(output, tuple) else (output, b"")
        return (-1 if code is None else int(code)), out or b"", err or b""

    def _probe(self) -> object:
        """A `Touch`, a script backend, or None when nothing can set a time."""
        try:
            if self._windows:
                code, _, _ = self._exec(
                    (
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        "exit 0",
                    )
                )
            else:
                code, _, _ = self._exec(("touch", "-c", "-d", "@0", "--", _PROBE_PATH))
        except NotImplementedError:
            return None
        except Exception as exc:
            if any(text in str(exc).lower() for text in _MISSING_PROGRAM):
                return None
            raise
        if code != 0:
            return None
        if not self._windows:
            return Touch(self._run_touch)
        from ..winrm._path import WinRMPathBackend

        return WinRMPathBackend(
            self._run_script, max_script_bytes=_WINDOWS_SCRIPT_BUDGET
        )

    def _resolved(self) -> object:
        if self._setter is _UNPROBED:
            self._setter = self._probe()
        return self._setter

    def supported(self) -> typing.Optional[bool]:
        """Whether this container can set a time; None when the probe failed."""
        try:
            return self._resolved() is not None
        except Exception:
            return None

    def utime(
        self,
        path: str,
        *,
        atime: typing.Optional[float],
        mtime: typing.Optional[float],
        require: typing.Callable[[str], object],
    ) -> None:
        """Set the times; `require(path)` raises for a missing path first."""
        setter = self._resolved()
        if setter is None:
            raise NotImplementedError(
                "this container has no touch or powershell.exe to set a time with"
            )
        if isinstance(setter, Touch):
            # `touch -c` exits 0 for a missing path.
            require(path)
            setter.utime(
                path, atime=atime, mtime=mtime, error=lambda text: _error(text, path)
            )
            return
        typing.cast(typing.Any, setter).utime(path, atime=atime, mtime=mtime)

    def _run_touch(self, argv: typing.Sequence[str]) -> typing.Tuple[int, str]:
        code, _, err = self._exec(argv)
        return code, err.decode("utf-8", "replace")

    def _run_script(self, script: str, check: bool = False, encoding="utf-8", **_):
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
        code, out, err = self._exec(argv)
        return subprocess.CompletedProcess(
            argv, code, out.decode(encoding, "replace"), err.decode(encoding, "replace")
        )


def _error(text: str, path: str) -> OSError:
    """The filesystem error a refused `touch` names in its message."""
    lowered = text.lower()
    for needle, error_type, code in (
        ("no such file", FileNotFoundError, errno.ENOENT),
        ("permission denied", PermissionError, errno.EACCES),
        ("operation not permitted", PermissionError, errno.EPERM),
        ("read-only file system", OSError, errno.EROFS),
    ):
        if needle in lowered:
            return error_type(code, text.strip(), path)
    return OSError(errno.EIO, text.strip() or "touch failed", path)
