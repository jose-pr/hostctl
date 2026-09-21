"""Persistent process adapter for AsyncSSH channels."""

from __future__ import annotations

import types
import typing
import inspect

from ._common import Process, ProcessData


class _Reader(typing.Protocol):
    def read(self, size: int = -1) -> typing.Awaitable[ProcessData]: ...


class _Writer(typing.Protocol):
    def write(self, data: ProcessData) -> None: ...

    def drain(self) -> typing.Awaitable[None]: ...

    def write_eof(self) -> None: ...


class _Completed(typing.Protocol):
    returncode: int


class _AsyncsshProcess(typing.Protocol):
    returncode: typing.Optional[int]
    stdin: _Writer
    stdout: _Reader
    stderr: _Reader

    def change_terminal_size(
        self, width: int, height: int, pixwidth: int, pixheight: int
    ) -> None: ...

    def close(self) -> None: ...

    def kill(self) -> None: ...

    def terminate(self) -> None: ...

    def wait(
        self, check: bool = False, timeout: typing.Optional[float] = None
    ) -> typing.Awaitable[_Completed]: ...

    def wait_closed(self) -> typing.Awaitable[None]: ...


class SshProcess(Process):
    """Synchronous facade over a process owned by hostctl's AsyncSSH loop."""

    def __init__(
        self,
        process: _AsyncsshProcess,
        command: typing.Optional[str],
        *,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> None:
        self._process = process
        self._command = command
        self._closed = False
        # The channel's mode: asyncssh gives str when an encoding was
        # requested at create_process() time and bytes otherwise.
        self._encoding = encoding
        self._errors = errors
        self._buffered_stdout: ProcessData = "" if encoding is not None else b""
        self._buffered_stderr: ProcessData = "" if encoding is not None else b""

    @property
    def returncode(self) -> typing.Optional[int]:
        return self._process.returncode

    def _call(self, function: typing.Callable[[], object]) -> object:
        from .. import _async

        async def invoke() -> object:
            result = function()
            if inspect.isawaitable(result):
                return await result
            return result

        try:
            return _async.async_to_sync(invoke())
        except Exception as exc:
            normalized = _async.normalize_asyncssh_error(exc, command=self._command)
            if normalized is exc:
                raise
            raise normalized from exc

    def write(self, data: ProcessData) -> None:
        async def operation() -> None:
            writer = self._process.stdin
            writer.write(self._encode(data))
            await writer.drain()

        self._call(operation)

    def _encode(self, data: ProcessData) -> ProcessData:
        """Match the channel's mode, rather than leaking asyncssh's TypeError.

        With no `encoding` the channel is binary, and asyncssh does
        `bytearray(data)` on a `str` -- so the documented
        `with host.shell as session: session.send("echo hi")` raised
        `TypeError: string argument without an encoding` from inside a third
        party. Container and serial adapters both encode here.
        """
        if isinstance(data, str) and self._encoding is None:
            return data.encode("utf-8")
        if isinstance(data, (bytes, bytearray)) and self._encoding is not None:
            return bytes(data).decode(self._encoding, self._errors or "strict")
        return data

    def read(self, size: int = -1) -> ProcessData:
        buffered = self._take_buffered("_buffered_stdout", size)
        if buffered:
            return buffered
        return self._call(lambda: self._process.stdout.read(size))

    def read_stderr(self, size: int = -1) -> ProcessData:
        buffered = self._take_buffered("_buffered_stderr", size)
        if buffered:
            return buffered
        return self._call(lambda: self._process.stderr.read(size))

    def send_eof(self) -> None:
        def operation() -> None:
            self._process.stdin.write_eof()

        self._call(operation)

    def resize(
        self,
        columns: int,
        rows: int,
        pixel_width: int = 0,
        pixel_height: int = 0,
    ) -> None:
        if columns <= 0 or rows <= 0:
            raise ValueError("terminal columns and rows must be positive")
        if pixel_width < 0 or pixel_height < 0:
            raise ValueError("terminal pixel dimensions must not be negative")
        self._call(
            lambda: self._process.change_terminal_size(
                columns, rows, pixel_width, pixel_height
            )
        )

    def wait(self, timeout: typing.Optional[float] = None) -> int:
        try:
            from .. import _async

            result = _async.async_to_sync(
                self._process.wait(check=False, timeout=timeout)
            )
        except Exception as exc:
            normalized = _async.normalize_asyncssh_error(
                exc, command=self._command, timeout=timeout
            )
            if normalized is exc:
                raise
            raise normalized from exc
        # asyncssh's wait() *clears* the receive buffers into the result. Only
        # the return code used to be kept, so every byte the caller had not
        # already read was destroyed and a later read() returned b"" --
        # indistinguishable from EOF. Hold it so read() can still serve it,
        # which is what ContainerProcess does.
        self._buffered_stdout += self._as_buffer(result.stdout)
        self._buffered_stderr += self._as_buffer(result.stderr)
        return -1 if result.returncode is None else result.returncode

    def _as_buffer(self, value: object) -> ProcessData:
        empty: ProcessData = "" if self._encoding is not None else b""
        if value is None:
            return empty
        if isinstance(value, str) and self._encoding is None:
            return value.encode("utf-8")
        if isinstance(value, (bytes, bytearray)) and self._encoding is not None:
            return bytes(value).decode(self._encoding, self._errors or "strict")
        return value

    def _take_buffered(self, name: str, size: int) -> ProcessData:
        buffered = getattr(self, name)
        if not buffered:
            return buffered
        if size is None or size < 0 or size >= len(buffered):
            setattr(self, name, buffered[:0])
            return buffered
        setattr(self, name, buffered[size:])
        return buffered[:size]

    def terminate(self) -> None:
        self._call(self._process.terminate)

    def kill(self) -> None:
        self._call(self._process.kill)

    def close(self) -> None:
        if self._closed:
            return
        # `_closed` is set only after both calls succeed, so a failed close
        # stays retryable and the caller keeps the channel reference. That is
        # the whole mechanism -- there used to be a `try/except: raise` around
        # this, which reads like it does something and does not.
        self._call(self._process.close)
        self._call(lambda: self._process.wait_closed())
        self._closed = True

    def __enter__(self) -> SshProcess:
        return self

    def __exit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[types.TracebackType],
    ) -> bool:
        self.close()
        return False
