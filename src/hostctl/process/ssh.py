"""Persistent process adapter for AsyncSSH channels."""

from __future__ import annotations

import types
import typing

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
        self, process: _AsyncsshProcess, command: typing.Optional[str]
    ) -> None:
        self._process = process
        self._command = command
        self._closed = False

    @property
    def returncode(self) -> typing.Optional[int]:
        return self._process.returncode

    def _call(self, function: typing.Callable[[], object]) -> object:
        from .. import _async

        async def invoke() -> object:
            return function()

        return _async.async_to_sync(invoke())

    def write(self, data: ProcessData) -> None:
        from .. import _async

        async def operation() -> None:
            writer = self._process.stdin
            writer.write(data)
            await writer.drain()

        _async.async_to_sync(operation())

    def read(self, size: int = -1) -> ProcessData:
        from .. import _async

        return _async.async_to_sync(self._process.stdout.read(size))

    def read_stderr(self, size: int = -1) -> ProcessData:
        from .. import _async

        return _async.async_to_sync(self._process.stderr.read(size))

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
        from .. import _async

        try:
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
        return result.returncode

    def terminate(self) -> None:
        self._call(self._process.terminate)

    def kill(self) -> None:
        self._call(self._process.kill)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._call(self._process.close)
        from .. import _async

        _async.async_to_sync(self._process.wait_closed())

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
