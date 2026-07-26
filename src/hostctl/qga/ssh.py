"""QGA Unix-socket transport tunneled through an AsyncSSH connection."""

from __future__ import annotations

import asyncio
import subprocess
import typing

from ._common import QgaDisconnectedError, QgaTimeoutError, _QgaFramedSession


class _Reader(typing.Protocol):
    def read(self, size: int) -> typing.Awaitable[bytes]: ...


class _Writer(typing.Protocol):
    def write(self, value: bytes) -> None: ...

    def drain(self) -> typing.Awaitable[None]: ...

    def close(self) -> None: ...


class _SshConnection(typing.Protocol):
    def open_unix_connection(
        self, path: str, *, encoding: typing.Optional[str] = None
    ) -> typing.Awaitable[typing.Tuple[_Reader, _Writer]]: ...


async def _wait(awaitable: typing.Awaitable[object], timeout: float) -> object:
    return await asyncio.wait_for(awaitable, timeout)


async def _close_writer(writer: _Writer) -> None:
    writer.close()
    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is not None:
        result = wait_closed()
        if hasattr(result, "__await__"):
            await result


class SshUnixGuestAgentTransport(_QgaFramedSession):
    """Persistent QGA stream opened on a remote Unix socket over SSH."""

    def __init__(
        self,
        path: str,
        connection: typing.Callable[[], _SshConnection],
        *,
        timeout: float = 10.0,
        max_reply_size: int = 8 * 1024 * 1024,
    ) -> None:
        if not path:
            raise ValueError("remote QGA socket path must not be empty")
        self.path = path
        self._connection = connection
        self._reader: typing.Optional[_Reader] = None
        self._writer: typing.Optional[_Writer] = None
        super().__init__(timeout=timeout, max_reply_size=max_reply_size)

    def __enter__(self) -> SshUnixGuestAgentTransport:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False

    def _connect_raw(self, deadline: float) -> None:
        from .. import _async

        try:
            opened = _async.async_to_sync(
                _wait(
                    self._connection().open_unix_connection(self.path, encoding=None),
                    self._remaining(deadline),
                )
            )
            self._reader, self._writer = typing.cast(
                typing.Tuple[_Reader, _Writer], opened
            )
        except Exception as exc:
            normalized = self._normalize_ssh_error(exc, "open QGA stream")
            if normalized is exc:
                raise
            raise normalized from exc

    def _send_raw(self, data: bytes, deadline: float) -> None:
        from .. import _async

        writer = self._require_writer()

        async def send() -> None:
            writer.write(data)
            await asyncio.wait_for(writer.drain(), self._remaining(deadline))

        try:
            _async.async_to_sync(send())
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise QgaTimeoutError("timed out sending a QGA request") from exc
        except Exception as exc:
            normalized = self._normalize_ssh_error(exc, "send QGA request")
            if normalized is exc:
                raise
            raise normalized from exc

    def _recv_raw(self, size: int, deadline: float) -> bytes:
        from .. import _async

        try:
            chunk = _async.async_to_sync(
                _wait(self._require_reader().read(size), self._remaining(deadline))
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise QgaTimeoutError("timed out waiting for a QGA reply") from exc
        except Exception as exc:
            normalized = self._normalize_ssh_error(exc, "receive QGA reply")
            if normalized is exc:
                raise
            raise normalized from exc
        if not isinstance(chunk, bytes):
            raise TypeError("SSH QGA stream returned non-byte data")
        return chunk

    def _close_raw(self) -> None:
        from .. import _async

        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            try:
                _async.async_to_sync(_close_writer(writer))
            except Exception:
                pass

    def _require_reader(self) -> _Reader:
        if self._reader is None:
            raise QgaDisconnectedError("QGA transport is not connected")
        return self._reader

    def _require_writer(self) -> _Writer:
        if self._writer is None:
            raise QgaDisconnectedError("QGA transport is not connected")
        return self._writer

    @staticmethod
    def _normalize_ssh_error(exc: Exception, command: str) -> Exception:
        from .. import _async

        normalized = _async.normalize_asyncssh_error(exc, command=command)
        if isinstance(normalized, subprocess.TimeoutExpired):
            return QgaTimeoutError(str(normalized))
        if isinstance(normalized, ConnectionError):
            return QgaDisconnectedError(str(normalized))
        return normalized
