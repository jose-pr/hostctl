"""Persistent process adapter for Docker Engine exec sockets."""

from __future__ import annotations

import collections
import socket
import subprocess
import time
import types
import typing

from ._common import Process, ProcessData


class ContainerExecApi(typing.Protocol):
    def exec_inspect(self, exec_id: str) -> typing.Mapping[str, object]: ...

    def exec_resize(self, exec_id: str, *, height: int, width: int) -> object: ...


class _Socket(typing.Protocol):
    def recv(self, size: int) -> bytes: ...

    def sendall(self, value: bytes) -> None: ...

    def shutdown(self, how: int) -> None: ...

    def close(self) -> None: ...


class ContainerProcess(Process):
    """Synchronous Docker exec channel, with framed non-TTY output."""

    def __init__(
        self,
        api: ContainerExecApi,
        exec_id: str,
        stream: object,
        *,
        tty: bool,
        command: typing.Sequence[str],
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> None:
        self._api = api
        self._exec_id = exec_id
        self._socket = typing.cast(_Socket, getattr(stream, "_sock", stream))
        self._tty = tty
        self._command = list(command)
        self._encoding = encoding
        self._errors = errors or "strict"
        self._buffers = {
            1: collections.deque(),  # type: typing.Deque[bytes]
            2: collections.deque(),  # type: typing.Deque[bytes]
        }
        self._eof = False
        self._closed = False

    @property
    def returncode(self) -> typing.Optional[int]:
        state = self._api.exec_inspect(self._exec_id)
        if state.get("Running"):
            return None
        value = state.get("ExitCode")
        return int(value) if value is not None else None

    def _encode(self, value: ProcessData) -> bytes:
        if isinstance(value, bytes):
            return value
        return value.encode(self._encoding or "utf-8", self._errors)

    def _decode(self, value: bytes) -> ProcessData:
        if self._encoding is None:
            return value
        return value.decode(self._encoding, self._errors)

    def write(self, data: ProcessData) -> None:
        if self._closed:
            raise ValueError("process is closed")
        self._socket.sendall(self._encode(data))

    def _receive(self) -> None:
        if self._eof:
            return
        if self._tty:
            value = self._socket.recv(64 * 1024)
            if value:
                self._buffers[1].append(value)
            else:
                self._eof = True
            return
        header = self._read_exact(8)
        if not header:
            self._eof = True
            return
        stream = header[0]
        length = int.from_bytes(header[4:8], "big")
        payload = self._read_exact(length)
        if stream in self._buffers and payload:
            self._buffers[stream].append(payload)

    def _read_exact(self, size: int) -> bytes:
        value = bytearray()
        while len(value) < size:
            chunk = self._socket.recv(size - len(value))
            if not chunk:
                break
            value.extend(chunk)
        return bytes(value)

    def _read_stream(self, stream: int, size: int) -> ProcessData:
        if self._tty and stream == 2:
            raise NotImplementedError("TTY sessions combine stdout and stderr")
        if size == 0:
            return self._decode(b"")
        output = bytearray()
        while size < 0 or len(output) < size:
            while not self._buffers[stream] and not self._eof:
                self._receive()
            if not self._buffers[stream]:
                break
            chunk = self._buffers[stream].popleft()
            if size >= 0 and len(output) + len(chunk) > size:
                take = size - len(output)
                output.extend(chunk[:take])
                self._buffers[stream].appendleft(chunk[take:])
            else:
                output.extend(chunk)
            if size >= 0 and len(output) >= size:
                break
        return self._decode(bytes(output))

    def read(self, size: int = -1) -> ProcessData:
        return self._read_stream(1, size)

    def read_stderr(self, size: int = -1) -> ProcessData:
        return self._read_stream(2, size)

    def send_eof(self) -> None:
        self._socket.shutdown(socket.SHUT_WR)

    def resize(
        self,
        columns: int,
        rows: int,
        pixel_width: int = 0,
        pixel_height: int = 0,
    ) -> None:
        if not self._tty:
            raise NotImplementedError("process does not have a terminal")
        if columns <= 0 or rows <= 0:
            raise ValueError("terminal columns and rows must be positive")
        if pixel_width < 0 or pixel_height < 0:
            raise ValueError("terminal pixel dimensions must not be negative")
        self._api.exec_resize(self._exec_id, height=rows, width=columns)

    def wait(self, timeout: typing.Optional[float] = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            returncode = self.returncode
            if returncode is not None:
                return returncode
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self._command, timeout)
            time.sleep(0.01)

    def terminate(self) -> None:
        raise NotImplementedError("Docker Engine cannot signal one exec process")

    def kill(self) -> None:
        raise NotImplementedError("Docker Engine cannot signal one exec process")

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._socket.close()

    def __enter__(self) -> ContainerProcess:
        return self

    def __exit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[types.TracebackType],
    ) -> bool:
        self.close()
        return False
