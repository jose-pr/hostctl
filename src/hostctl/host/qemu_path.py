"""Guest filesystem paths backed by QEMU Guest Agent file RPCs."""

from __future__ import annotations

import base64
import binascii
import io
import typing
import uuid

from pathlib import PurePath as _StdPurePath
from pathlib_next import Path, PosixPathname, WindowsPathname
from pathlib_next.utils.stat import FileStat

from ..qga._common import (
    GuestAgentTransport,
    QgaDisconnectedError,
    QgaTimeoutError,
)


class GuestPathHelper(typing.Protocol):
    """Positively probed OS helper operations not supplied by QGA file RPCs."""

    def stat(self, path: str, *, follow_symlinks: bool = True) -> FileStat: ...

    def scandir(self, path: str) -> typing.Iterable[typing.Tuple[str, FileStat]]: ...

    def mkdir(self, path: str, mode: int) -> None: ...

    def unlink(self, path: str, *, missing_ok: bool = False) -> None: ...

    def rmdir(self, path: str) -> None: ...

    def rename(self, path: str, target: str, *, replace: bool = False) -> None: ...

    def chmod(self, path: str, mode: int, *, follow_symlinks: bool = True) -> None: ...


def _qga_error(exc: Exception, path: str) -> OSError:
    """Translate transport-specific QGA errors without importing a provider."""
    if isinstance(exc, (QgaTimeoutError, QgaDisconnectedError)):
        return exc
    name = str(
        getattr(exc, "error_class", "")
        or getattr(exc, "name", "")
        or getattr(exc, "code", "")
        or type(exc).__name__
    ).lower()
    message = (
        str(getattr(exc, "description", "") or getattr(exc, "message", "") or exc)
        or path
    )
    detail = f"{name} {message.lower()}"
    if any(
        value in detail
        for value in (
            "notfound",
            "enoent",
            "filenotfound",
            "no such file or directory",
            "cannot find the file",
            "cannot find the path",
        )
    ):
        return FileNotFoundError(message)
    if any(
        value in detail
        for value in ("permission", "denied", "eacces", "access is denied")
    ):
        return PermissionError(message)
    if any(value in detail for value in ("eexist", "already exists", "file exists")):
        return FileExistsError(message)
    if any(value in detail for value in ("isdir", "eisdir", "is a directory")):
        return IsADirectoryError(message)
    return OSError(message)


class QgaPathBackend:
    """Bounded file transfer plus capability-gated guest path helpers."""

    chunk_size = 48 * 1024
    _READ_COMMANDS = frozenset(
        {"guest-file-open", "guest-file-read", "guest-file-close"}
    )
    _WRITE_COMMANDS = frozenset(
        {
            "guest-file-open",
            "guest-file-write",
            "guest-file-flush",
            "guest-file-close",
        }
    )

    def __init__(
        self,
        transport: GuestAgentTransport,
        *,
        supported_commands: typing.Iterable[str],
        helper: typing.Optional[GuestPathHelper] = None,
        timeout: typing.Optional[float] = None,
        chunk_size: int = chunk_size,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.transport = transport
        self.supported_commands = frozenset(supported_commands)
        self.helper = helper
        self.timeout = timeout
        self.chunk_size = chunk_size

    def _require(self, commands: typing.Iterable[str]) -> None:
        missing = set(commands) - self.supported_commands
        if missing:
            values = ", ".join(sorted(missing))
            raise NotImplementedError(f"QGA commands are unavailable: {values}")

    def _execute(
        self, command: str, arguments: typing.Optional[dict] = None, *, path: str
    ) -> object:
        self._require((command,))
        request: typing.Dict[str, object] = {"execute": command}
        if arguments is not None:
            request["arguments"] = arguments
        try:
            value = self.transport.execute(request, timeout=self.timeout)
        except Exception as exc:
            raise _qga_error(exc, path) from exc
        if isinstance(value, dict) and set(value) == {"return"}:
            return value["return"]
        return value

    def _open(self, path: str, mode: str) -> int:
        value = self._execute(
            "guest-file-open", {"path": path, "mode": mode}, path=path
        )
        if isinstance(value, dict):
            value = value.get("handle")
        if not isinstance(value, int):
            raise OSError("QGA guest-file-open returned an invalid handle")
        return value

    def _close(self, handle: int, path: str) -> None:
        self._execute("guest-file-close", {"handle": handle}, path=path)

    def seek(
        self,
        handle: int,
        offset: int,
        whence: typing.Union[str, int] = "set",
        *,
        path: str,
    ) -> int:
        """Seek an open QGA file handle and return its resulting position."""
        if isinstance(whence, str):
            if whence not in {"set", "cur", "end"}:
                raise ValueError(f"invalid seek origin: {whence!r}")
            whence_value: object = {"name": whence}
        elif isinstance(whence, int) and whence in (0, 1, 2):
            whence_value = whence
        else:
            raise ValueError(f"invalid seek origin: {whence!r}")
        value = self._execute(
            "guest-file-seek",
            {
                "handle": handle,
                "offset": offset,
                "whence": whence_value,
            },
            path=path,
        )
        if not isinstance(value, dict) or not isinstance(value.get("position"), int):
            raise OSError("QGA guest-file-seek returned an invalid position")
        return typing.cast(int, value["position"])

    def read_bytes(self, path: str) -> bytes:
        self._require(self._READ_COMMANDS)
        handle = self._open(path, "rb")
        chunks: typing.List[bytes] = []
        failed = False
        try:
            while True:
                value = self._execute(
                    "guest-file-read",
                    {"handle": handle, "count": self.chunk_size},
                    path=path,
                )
                if not isinstance(value, dict):
                    raise OSError("QGA guest-file-read returned invalid data")
                encoded = value.get("buf-b64", "")
                if not isinstance(encoded, str):
                    raise OSError("QGA guest-file-read returned invalid content")
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise OSError(
                        "QGA guest-file-read returned invalid Base64"
                    ) from exc
                count = value.get("count", len(data))
                if not isinstance(count, int) or count != len(data):
                    raise OSError("QGA guest-file-read returned an invalid count")
                eof = value.get("eof", False)
                if not isinstance(eof, bool):
                    raise OSError("QGA guest-file-read returned an invalid EOF flag")
                chunks.append(data)
                if eof or count == 0:
                    break
        except BaseException:
            failed = True
            raise
        finally:
            try:
                self._close(handle, path)
            except Exception:
                if not failed:
                    raise
        return b"".join(chunks)

    def _write_direct(self, path: str, value: bytes) -> None:
        self._require(self._WRITE_COMMANDS)
        handle = self._open(path, "wb")
        failed = False
        try:
            for offset in range(0, len(value), self.chunk_size):
                pending = value[offset : offset + self.chunk_size]
                while pending:
                    encoded = base64.b64encode(pending).decode("ascii")
                    result = self._execute(
                        "guest-file-write",
                        {"handle": handle, "buf-b64": encoded},
                        path=path,
                    )
                    if not isinstance(result, dict):
                        raise OSError("QGA guest-file-write returned invalid data")
                    count = result.get("count", len(pending))
                    if not isinstance(count, int) or count <= 0 or count > len(pending):
                        raise OSError("QGA guest-file-write returned an invalid count")
                    pending = pending[count:]
            self._execute("guest-file-flush", {"handle": handle}, path=path)
        except BaseException:
            failed = True
            raise
        finally:
            try:
                self._close(handle, path)
            except Exception:
                if not failed:
                    raise

    def write_bytes(self, path: str, value: bytes, *, exclusive: bool = False) -> None:
        if self.helper is None:
            if exclusive:
                raise NotImplementedError(
                    "exclusive QGA writes require a transactional guest helper"
                )
            self._write_direct(path, value)
            return
        temporary = f"{path}.hostctl-{uuid.uuid4().hex}"
        try:
            self._write_direct(temporary, value)
            self._helper_method("rename")(temporary, path, replace=not exclusive)
        except Exception:
            try:
                self._helper_method("unlink")(temporary, missing_ok=True)
            except Exception:
                pass
            raise

    def _helper_method(self, operation: str):
        if self.helper is None:
            raise NotImplementedError(
                f"QGA {operation} requires a positively probed guest helper"
            )
        method = getattr(self.helper, operation, None)
        if not callable(method):
            raise NotImplementedError(f"QGA helper does not support {operation}")
        return method

    def stat(self, path: str, *, follow_symlinks: bool = True) -> FileStat:
        return self._helper_method("stat")(path, follow_symlinks=follow_symlinks)

    def scandir(self, path: str) -> typing.List[typing.Tuple[str, FileStat]]:
        return list(self._helper_method("scandir")(path))

    def mkdir(self, path: str, mode: int) -> None:
        self._helper_method("mkdir")(path, mode)

    def unlink(self, path: str, *, missing_ok: bool = False) -> None:
        self._helper_method("unlink")(path, missing_ok=missing_ok)

    def rmdir(self, path: str) -> None:
        self._helper_method("rmdir")(path)

    def rename(self, path: str, target: str) -> None:
        self._helper_method("rename")(path, target)

    def chmod(self, path: str, mode: int, *, follow_symlinks: bool = True) -> None:
        self._helper_method("chmod")(path, mode, follow_symlinks=follow_symlinks)


class _WriteBackBytesIO(io.BytesIO):
    def __init__(
        self, value: bytes, commit: typing.Optional[typing.Callable[[bytes], None]]
    ) -> None:
        super().__init__(value)
        self._commit = commit

    def close(self) -> None:
        if not self.closed and self._commit is not None:
            value = self.getvalue()
            commit, self._commit = self._commit, None
            try:
                commit(value)
            finally:
                super().close()
        else:
            super().close()


class _QgaPathMixin:
    __slots__ = ()

    @property
    def backend(self) -> QgaPathBackend:
        return self._backend

    def with_segments(self, *segments: str):
        return type(self)(*segments, backend=self.backend)

    def __truediv__(self, key):
        return type(self)(self, key, backend=self.backend)

    def joinpath(self, *args):
        return type(self)(self, *args, backend=self.backend)

    @property
    def parent(self):
        return type(self)(str(super().parent), backend=self.backend)

    def stat(self, *, follow_symlinks: bool = True) -> FileStat:
        return self.backend.stat(str(self), follow_symlinks=follow_symlinks)

    def _scandir(self):
        yield from self.backend.scandir(str(self))

    def iterdir(self):
        for name, _ in self._scandir():
            yield self / name

    def _open(self, mode="r", buffering=-1):
        if (
            not mode
            or sum(mode.count(value) for value in "rwax") != 1
            or mode.count("+") > 1
            or len(mode) != 1 + mode.count("+")
        ):
            raise ValueError(f"invalid mode: {mode!r}")
        readable = "r" in mode or "+" in mode
        writable = any(value in mode for value in "wax+")
        if "r" in mode or "a" in mode:
            try:
                value = self.backend.read_bytes(str(self))
            except FileNotFoundError:
                if "a" in mode:
                    value = b""
                else:
                    raise
        else:
            value = b""
        stream = _WriteBackBytesIO(
            value,
            (
                (
                    lambda data: self.backend.write_bytes(
                        str(self), data, exclusive="x" in mode
                    )
                )
                if writable
                else None
            ),
        )
        if "a" in mode:
            stream.seek(0, io.SEEK_END)
        elif not readable:
            stream.seek(0)
        return stream

    def _mkdir(self, mode: int):
        self.backend.mkdir(str(self), mode)

    def chmod(self, mode: int, *, follow_symlinks: bool = True):
        self.backend.chmod(str(self), mode, follow_symlinks=follow_symlinks)

    def unlink(self, missing_ok=False):
        self.backend.unlink(str(self), missing_ok=missing_ok)

    def rmdir(self):
        self.backend.rmdir(str(self))

    def rename(self, target):
        if not isinstance(target, _QgaPathMixin):
            target = type(self)(target, backend=self.backend)
        if target.backend is not self.backend:
            raise ValueError("cannot rename across QGA path backends")
        self.backend.rename(str(self), str(target))
        return target


class PosixQemuPath(_QgaPathMixin, PosixPathname, Path):
    """A POSIX guest path backed by QEMU Guest Agent."""

    __slots__ = ("_backend",)

    def __init__(self, *segments, backend=None):
        # Python 3.14's pathlib.PurePath.__init__ no longer accepts kwargs.
        # Path state is initialized by __new__; backend is attached there.
        if not hasattr(self, "_raw_paths") and not hasattr(self, "_parts"):
            _StdPurePath.__init__(self, *segments)

    def __new__(
        cls,
        *segments: typing.Union[str, PosixPathname],
        backend: typing.Optional[QgaPathBackend] = None,
    ):
        inherited = next(
            (
                segment.backend
                for segment in segments
                if isinstance(segment, _QgaPathMixin)
            ),
            None,
        )
        self = super().__new__(cls, *segments)
        self._backend = backend or inherited
        if self._backend is None:
            raise TypeError("PosixQemuPath requires a backend")
        return self


class WindowsQemuPath(_QgaPathMixin, WindowsPathname, Path):
    """A Windows guest path backed by QEMU Guest Agent."""

    __slots__ = ("_backend",)

    def __init__(self, *segments, backend=None):
        # Python 3.14's pathlib.PurePath.__init__ no longer accepts kwargs.
        # Path state is initialized by __new__; backend is attached there.
        if not hasattr(self, "_raw_paths") and not hasattr(self, "_parts"):
            _StdPurePath.__init__(self, *segments)

    def __new__(
        cls,
        *segments: typing.Union[str, WindowsPathname],
        backend: typing.Optional[QgaPathBackend] = None,
    ):
        inherited = next(
            (
                segment.backend
                for segment in segments
                if isinstance(segment, _QgaPathMixin)
            ),
            None,
        )
        self = super().__new__(cls, *segments)
        self._backend = backend or inherited
        if self._backend is None:
            raise TypeError("WindowsQemuPath requires a backend")
        return self
