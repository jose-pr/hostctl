"""Guest files over the QEMU Guest Agent file RPCs."""

from __future__ import annotations

import base64

import binascii


import io


import typing

import warnings

import uuid


from pathapi import FileStat


from ...exceptions import QGAProtocolError


from ._qga import GuestAgentTransport, guest_oserror


from ._helper import GuestPathHelper


class QGAPathBackend:
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
        self._closed = False
        self.supported_commands = frozenset(supported_commands)
        self.helper = helper
        self.timeout = timeout
        self.chunk_size = chunk_size

    def invalidate(self) -> None:
        """Refuse further work: the host that owned this backend is closed."""
        self._closed = True

    def _require(self, commands: typing.Iterable[str]) -> None:
        if self._closed:
            raise ValueError("the QEMU host that owns this path is closed")
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
            raise guest_oserror(exc, path) from exc
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

    def _read_chunk(
        self, handle: int, count: int, *, path: str
    ) -> typing.Tuple[bytes, bool]:
        """One `guest-file-read` round trip: the bytes and whether EOF came."""
        value = self._execute(
            "guest-file-read",
            {"handle": handle, "count": count},
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
            raise OSError("QGA guest-file-read returned invalid Base64") from exc
        if len(data) > count:
            raise QGAProtocolError(
                "QGA guest-file-read returned more bytes than requested"
            )
        actual = value.get("count", len(data))
        if not isinstance(actual, int) or actual != len(data):
            raise OSError("QGA guest-file-read returned an invalid count")
        eof = value.get("eof", False)
        if not isinstance(eof, bool):
            raise OSError("QGA guest-file-read returned an invalid EOF flag")
        return data, eof or not data

    def read_bytes(self, path: str) -> bytes:
        self._require(self._READ_COMMANDS)
        handle = self._open(path, "rb")
        chunks: typing.List[bytes] = []
        failed = False
        try:
            while True:
                data, eof = self._read_chunk(handle, self.chunk_size, path=path)
                chunks.append(data)
                if eof:
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

    def read_handle(
        self, handle: int, count: int, *, path: str
    ) -> typing.Tuple[bytes, bool]:
        if count <= 0:
            return b"", True
        return self._read_chunk(handle, count, path=path)

    def open_read(self, path: str) -> io.BufferedReader:
        self._require(self._READ_COMMANDS)
        return io.BufferedReader(_QGAReadStream(self, path))

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
        if not exclusive:
            # In place, like a local `open(path, "w")`: the file keeps its
            # inode, mode, owner and any symlink, and a Windows guest (whose
            # rename cannot replace) writes at all.
            self._write_direct(path, value)
            return
        if self.helper is None:
            raise NotImplementedError(
                "exclusive QGA writes require a transactional guest helper"
            )
        temporary = f"{path}.hostctl-{uuid.uuid4().hex}"
        try:
            self._write_direct(temporary, value)
            self._helper_method("rename")(temporary, path, replace=False)
        except Exception:
            try:
                self._helper_method("unlink")(temporary, missing_ok=True)
            except Exception:
                pass
            raise

    def _helper_method(self, operation: str):
        if self._closed:
            raise ValueError("the QEMU host that owns this path is closed")
        if self.helper is None:
            raise NotImplementedError(
                f"QGA {operation} requires a positively probed guest helper"
            )
        method = getattr(self.helper, operation, None)
        if not callable(method):
            raise NotImplementedError(f"QGA helper does not support {operation}")
        return method

    def exists(self, path: str, *, follow_symlinks: bool = True) -> bool:
        """Answer existence with whatever the guest agent actually offers.

        `stat` needs a probed helper, but existence does not: opening a path
        for reading distinguishes "absent" from "there but unreadable" using
        only the file RPCs every QGA build has. Without this, `exists()` on
        every host hostctl can build raised `NotImplementedError` instead of
        returning a boolean, which `docs/guide/contracts.md` requires -- and
        that is what made `Path.copy()` into a guest impossible.
        """
        if self.helper is not None:
            try:
                self.stat(path, follow_symlinks=follow_symlinks)
            except (FileNotFoundError, NotADirectoryError):
                return False
            return True
        try:
            handle = self._open(path, "r")
        except FileNotFoundError:
            return False
        except NotImplementedError:
            raise
        except OSError:
            # Permission denied, EISDIR on a Windows guest: something is
            # there, it just cannot be opened this way.
            return True
        try:
            return True
        finally:
            try:
                self._close(handle, path)
            except Exception:
                pass

    def stat(self, path: str, *, follow_symlinks: bool = True) -> FileStat:
        if self.helper is None and not self.exists(path):
            # Absence the file RPCs can prove is reported as absence, not as
            # "this build cannot describe files". Only that distinction lets
            # a generic `Path.copy()` push a file into a helper-less guest:
            # it reads FileNotFoundError as "create it" and anything else as
            # a failure. An entry that *is* there still raises
            # NotImplementedError below -- hostctl knows it exists and
            # genuinely cannot describe it.
            raise FileNotFoundError(path)
        return self._helper_method("stat")(path, follow_symlinks=follow_symlinks)

    def scandir(self, path: str) -> typing.List[typing.Tuple[str, FileStat]]:
        return list(self._helper_method("scandir")(path))

    def mkdir(self, path: str, mode: int) -> None:
        self._helper_method("mkdir")(path, mode)

    def unlink(self, path: str, *, missing_ok: bool = False) -> None:
        self._helper_method("unlink")(path, missing_ok=missing_ok)

    def rmdir(self, path: str) -> None:
        self._helper_method("rmdir")(path)

    def rename(self, path: str, target: str, *, replace: bool = False) -> None:
        self._helper_method("rename")(path, target, replace=replace)

    def chmod(self, path: str, mode: int, *, follow_symlinks: bool = True) -> None:
        self._helper_method("chmod")(path, mode, follow_symlinks=follow_symlinks)

    def symlink(self, path: str, target: str) -> None:
        raise NotImplementedError(
            "QEMU Guest Agent has no symlink RPC; the guest-file-* commands "
            "only open, read, write, seek, flush, and close regular files"
        )

    def readlink(self, path: str) -> str:
        raise NotImplementedError(
            "QEMU Guest Agent has no readlink RPC; the guest-file-* commands "
            "only open, read, write, seek, flush, and close regular files"
        )


class _QGAReadStream(io.RawIOBase):
    """Lazy QGA guest-file-read stream with one bounded request per fill."""

    def __init__(self, backend: QGAPathBackend, path: str) -> None:
        self._backend = backend
        self._path = path
        self._handle: typing.Optional[int] = None
        self._handle = backend._open(path, "rb")
        self._eof = False

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        # The backend has implemented `guest-file-seek` all along and the
        # documentation advertised it, but nothing exposed it: `open("rb")`
        # answered `seekable() == False`, so `zipfile`, `tarfile` and every
        # other reader that looks at the end of a file raised
        # `io.UnsupportedOperation` on a guest path.
        return "guest-file-seek" in self._backend.supported_commands

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if self._handle is None:
            raise ValueError("seek of a closed QGA read stream")
        if not self.seekable():
            raise io.UnsupportedOperation(
                "this guest agent does not support guest-file-seek"
            )
        position = self._backend.seek(self._handle, offset, whence, path=self._path)
        # After a seek the position is not known to be at the end.
        self._eof = False
        return position

    def tell(self) -> int:
        if self._handle is None:
            raise ValueError("tell of a closed QGA read stream")
        return self.seek(0, io.SEEK_CUR)

    def readinto(self, target: bytearray) -> int:
        if self._eof or not target:
            return 0
        data, eof = self._backend.read_handle(
            typing.cast(int, self._handle),
            min(len(target), self._backend.chunk_size),
            path=self._path,
        )
        if data:
            target[: len(data)] = data
        self._eof = eof
        return len(data)

    def close(self) -> None:
        if not self.closed and self._handle is not None:
            handle, self._handle = self._handle, None
            try:
                self._backend._close(handle, self._path)
            finally:
                super().close()
        else:
            super().close()

    def __del__(self) -> None:
        # Never issue the guest RPC from the collector. `RawIOBase.__del__`
        # calls `close()`, and `close()` sends `guest-file-close`; the framed
        # session's lock is not reentrant, so a collection firing inside
        # `execute()` on the same thread deadlocked the transport for good.
        # Dropping the handle leaks one guest-side file handle, which is the
        # lesser harm and is what the warning is for.
        if not self.closed and self._handle is not None:
            warnings.warn(
                f"unclosed QGA read stream for {self._path}: the guest handle "
                "was not released",
                ResourceWarning,
                stacklevel=2,
            )
            self._handle = None
        try:
            super().close()
        except Exception:
            pass
