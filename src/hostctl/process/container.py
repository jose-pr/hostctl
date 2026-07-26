"""Persistent process adapter for Docker Engine exec sockets."""

from __future__ import annotations

import collections
import codecs
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
        self._returncode: typing.Optional[int] = None
        self._wire_buffer = bytearray()
        self._decoders = {
            1: (
                codecs.getincrementaldecoder(encoding)(self._errors)
                if encoding
                else None
            ),
            2: (
                codecs.getincrementaldecoder(encoding)(self._errors)
                if encoding
                else None
            ),
        }
        settimeout = getattr(self._socket, "settimeout", None)
        if settimeout is not None:
            settimeout(None)

    @property
    def returncode(self) -> typing.Optional[int]:
        if self._returncode is not None:
            return self._returncode
        state = self._api.exec_inspect(self._exec_id)
        if state.get("Running"):
            return None
        value = state.get("ExitCode")
        if value is None:
            return None
        self._returncode = int(value)
        return self._returncode

    def _encode(self, value: ProcessData) -> bytes:
        if isinstance(value, bytes):
            return value
        return value.encode(self._encoding or "utf-8", self._errors)

    def _decode(
        self, value: bytes, stream: int = 1, *, final: bool = False
    ) -> ProcessData:
        if self._encoding is None:
            return value
        decoder = self._decoders[stream]
        return typing.cast(codecs.IncrementalDecoder, decoder).decode(
            value, final=final
        )

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
        while not self._consume_frames():
            chunk = self._socket.recv(64 * 1024)
            if not chunk:
                self._finish_wire()
                return
            self._wire_buffer.extend(chunk)

    def _consume_frames(self) -> bool:
        """Consume every complete Docker multiplex frame already buffered."""
        consumed = False
        while len(self._wire_buffer) >= 8:
            length = int.from_bytes(self._wire_buffer[4:8], "big")
            frame_size = 8 + length
            if len(self._wire_buffer) < frame_size:
                break
            stream = self._wire_buffer[0]
            payload = bytes(self._wire_buffer[8:frame_size])
            del self._wire_buffer[:frame_size]
            if stream in self._buffers and payload:
                self._buffers[stream].append(payload)
            consumed = True
        return consumed

    def _finish_wire(self) -> None:
        self._eof = True
        if self._wire_buffer:
            raise ConnectionError("connection dropped mid-frame")

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
            # read(n) returns as soon as data is available, like SSH and
            # socket streams; callers needing exactly n bytes can loop.
            if size >= 0 and output:
                break
        return self._decode(bytes(output), stream)

    def read(self, size: int = -1) -> ProcessData:
        return self._read_stream(1, size)

    def read_stderr(self, size: int = -1) -> ProcessData:
        return self._read_stream(2, size)

    def send_eof(self) -> None:
        shutdown = getattr(self._socket, "shutdown", None)
        if shutdown is None:
            raise NotImplementedError("exec transport cannot half-close stdin")
        try:
            shutdown(socket.SHUT_WR)
        except (OSError, ValueError) as exc:
            raise NotImplementedError("exec transport cannot half-close stdin") from exc

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
            # Drain one available frame before polling.  Non-blocking Docker
            # sockets raise timeout/BlockingIOError when no data is ready.
            self._receive_available()
            returncode = self.returncode
            if returncode is not None:
                return returncode
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self._command, timeout)
            time.sleep(0.01)

    def _receive_available(self) -> None:
        settimeout = getattr(self._socket, "settimeout", None)
        gettimeout = getattr(self._socket, "gettimeout", None)
        if settimeout is None:
            # There is no portable way to prove that recv() will not block.
            # wait() must keep polling process state rather than deadlock.
            return
        previous = gettimeout() if gettimeout is not None else None
        try:
            settimeout(0.0)
            while not self._eof:
                try:
                    value = self._socket.recv(64 * 1024)
                except (socket.timeout, BlockingIOError):
                    break
                if not value:
                    if self._tty:
                        self._eof = True
                    else:
                        self._finish_wire()
                    break
                if self._tty:
                    self._buffers[1].append(value)
                else:
                    self._wire_buffer.extend(value)
                    self._consume_frames()
        finally:
            settimeout(previous)

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
