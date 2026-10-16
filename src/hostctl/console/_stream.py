"""The byte stream a console profile drives, and an adapter for any process."""

from __future__ import annotations

import inspect
import threading
import typing


@typing.runtime_checkable
class ConsoleStream(typing.Protocol):
    """A console's merged byte stream, as a profile drives it.

    `read` returns up to `size` bytes (`-1`: whatever is available), or `b""`
    when nothing arrived within `timeout` seconds: `None` is the stream's own
    wait, `0` returns only what is already there. It raises `ConnectionError`
    once the stream has ended. `write` delivers all of `data` or raises.

    A stream may also offer `reset_input_buffer()`, which a profile calls to
    discard bytes nobody is waiting for.
    """

    def read(self, size: int = -1, *, timeout: typing.Optional[float] = None) -> bytes:
        raise NotImplementedError(f"{type(self).__name__}.read")

    def write(self, data: bytes) -> None:
        raise NotImplementedError(f"{type(self).__name__}.write")


def _takes_timeout(read: typing.Callable[..., object]) -> bool:
    try:
        parameter = inspect.signature(read).parameters.get("timeout")
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


def console_stream(process: typing.Any) -> ConsoleStream:
    """`process` as a `ConsoleStream` a profile can drive with a deadline.

    A process whose `read` already takes `timeout=` (a `SerialProcess`, a
    serial `spawn()` session) is returned as it is. Any other byte-mode
    `Process` -- a QEMU console from `open_serial()`, a container or SSH
    session opened with `terminal=True` -- is read by a background thread
    from then on, so read it only through the returned stream: its `read`
    must block until data arrives and return `b""` only at the end.
    """
    if _takes_timeout(process.read):
        return typing.cast(ConsoleStream, process)
    return _ReaderThreadStream(process)


class _ReaderThreadStream:
    """A blocking process read on a thread, so a read can be given a deadline."""

    _chunk = 64 * 1024

    def __init__(self, process: typing.Any) -> None:
        self._process = process
        self._buffer = bytearray()
        self._ready = threading.Condition()
        self._ended = False
        self._error: typing.Optional[BaseException] = None
        self._thread = threading.Thread(
            target=self._pump, name="hostctl-console-reader", daemon=True
        )
        self._thread.start()

    def _pump(self) -> None:
        try:
            while True:
                data = self._process.read(self._chunk)
                if isinstance(data, str):
                    raise TypeError(
                        "console_stream needs a process that reads bytes; "
                        "open it without encoding= or errors="
                    )
                with self._ready:
                    if not data:
                        self._ended = True
                        self._ready.notify_all()
                        return
                    self._buffer += data
                    self._ready.notify_all()
        except Exception as exc:
            with self._ready:
                self._error = exc
                self._ended = True
                self._ready.notify_all()

    def read(self, size: int = -1, *, timeout: typing.Optional[float] = None) -> bytes:
        if size < -1:
            raise ValueError("read size must be -1 or non-negative")
        if timeout is not None and timeout < 0:
            raise ValueError("read timeout must not be negative")
        if size == 0:
            return b""
        with self._ready:
            self._ready.wait_for(lambda: self._buffer or self._ended, timeout)
            if self._buffer:
                take = len(self._buffer) if size == -1 else size
                data = bytes(self._buffer[:take])
                del self._buffer[:take]
                return data
            if self._ended:
                if isinstance(self._error, TypeError):
                    raise self._error
                raise ConnectionError("console stream ended") from self._error
            return b""

    def write(self, data: bytes) -> None:
        self._process.write(data)

    def reset_input_buffer(self) -> None:
        with self._ready:
            self._buffer.clear()


__all__ = ["ConsoleStream", "console_stream"]
