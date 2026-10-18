"""`run()` over a POSIX shell's console: an executor a provider can wrap."""

from __future__ import annotations

import threading
import time
import typing

from ..exceptions import OperationNotStartedError
from ..executor import ExecutorRequest, StreamingExecutor, expired
from ..provider._selector import ProviderSelector
from ._posix import PosixConsoleProfile, _Frame
from ._stream import ConsoleStream


class _Running:
    """The command that holds the terminal, and how far it got."""

    def __init__(self, request: ExecutorRequest) -> None:
        self.request = request
        self.stream: typing.Optional[ConsoleStream] = None
        self.frame: typing.Optional[_Frame] = None
        #: The command line was written, or a write of it was attempted.
        self.started = False
        self.pending: typing.Optional[typing.Tuple[bytes, bytes]] = None


class ConsoleExecutor(StreamingExecutor):
    """A subprocess-shaped executor over a POSIX shell's console.

    `stream_factory()` returns the `ConsoleStream` of a console whose shell
    is ready to type into (log in there, with `profile.negotiate(stream)`,
    when it needs it); it is called once per command. `profile` frames each
    command (default `PosixConsoleProfile()`; pass one with `batch=None` for
    a stream that cannot overflow). Wrap it in an `ExecutorProvider`: it
    takes no native `args`, `cwd` or `env`, so a host renders one line of
    shell text, which runs in a child shell of the console's.

    Output is delivered as it arrives: stdout during the command, stderr
    when it ends. One command holds the terminal from its first typed byte
    until its status arrives; another call waits for it, within its own
    timeout. `OperationNotStartedError` (a provider decline) is raised only
    before the command line is typed: when `stream_factory` raises `OSError`
    or `TimeoutError`, or the stream fails while the command's files are
    typed. A failure after that is a `ConnectionError`.

    After a timeout, and after a failure once the stream was obtained (a
    decline included), `_abandon(request, stream=)` types ^C on that stream.
    A timeout raises `TimeoutExpired` with `orphaned=True`: ^C stops only a
    program that heeds it. Override `_abandon` to also drop `stream`,
    returning `False` when that ends the command (closing a login session
    hangs up its jobs). It runs before the terminal is released, so no other
    command has typed into `stream`.
    """

    executor_capabilities: typing.FrozenSet[str] = frozenset()

    def __init__(
        self,
        stream_factory: typing.Callable[[], ConsoleStream],
        *,
        profile: typing.Optional[PosixConsoleProfile] = None,
    ) -> None:
        if profile is None:
            profile = PosixConsoleProfile()
        if not isinstance(profile, PosixConsoleProfile):
            raise TypeError(
                "ConsoleExecutor frames commands with a PosixConsoleProfile, "
                f"not {type(profile).__name__}"
            )
        self.stream_factory = stream_factory
        self.profile = profile
        self._terminal = threading.Lock()
        self._running: typing.Optional[_Running] = None

    def _send(self, request: ExecutorRequest) -> None:
        wait = request.remaining()
        if not self._terminal.acquire(timeout=-1 if wait is None else max(wait, 0)):
            raise expired(request.command, request.timeout)
        running = self._running = _Running(request)
        try:
            running.stream = self.stream_factory()
        except OperationNotStartedError:
            self._release(request)
            raise
        except (OSError, TimeoutError) as exc:
            self._release(request)
            raise OperationNotStartedError(
                "console stream unavailable: "
                f"{type(exc).__name__}: {ProviderSelector.redact(exc)}",
                cause=exc,
            ) from exc
        except BaseException:
            self._release(request)
            raise
        try:
            frame, leftover, line = self.profile._prepare(
                running.stream,
                request.command,
                input=request.input,
                merge_stderr=request.merge_stderr,
                deadline=request.deadline,
            )
        except OperationNotStartedError:
            self._stop(request)
            raise
        except TimeoutError as exc:
            raise expired(request.command, request.timeout) from exc
        running.started = True
        running.stream.write(line)
        running.frame = frame
        running.pending = frame.feed(leftover)

    def _receive(
        self, request: ExecutorRequest, wait: typing.Optional[float]
    ) -> typing.Tuple[bytes, bytes, typing.Optional[int]]:
        running = self._owned(request)
        frame = typing.cast(_Frame, running.frame)
        if running.pending is not None:
            (out, err), running.pending = running.pending, None
        else:
            stream = typing.cast(ConsoleStream, running.stream)
            chunk = stream.read(self.profile.read_size, timeout=wait)
            if not chunk:
                # A stream may return at once with nothing; do not spin on it.
                time.sleep(0.001)
                return b"", b"", None
            out, err = frame.feed(chunk)
        if frame.state != "done":
            return out, err, None
        self._release(request)
        return out, err, frame.returncode

    def _abandon(
        self, request: ExecutorRequest, *, stream: typing.Optional[ConsoleStream] = None
    ) -> bool:
        """Type ^C on `stream`, the command's terminal; True: it may still run.

        `stream` defaults to the running command's. A write that fails is
        ignored: a stream is often abandoned because it already failed.
        """
        if stream is None:
            running = self._running
            if running is not None and running.request is request:
                stream = running.stream
        if stream is not None:
            try:
                stream.write(b"\x03")
            except Exception:
                pass
        return True

    def _stop(self, request: ExecutorRequest) -> bool:
        running = self._running
        if running is None or running.request is not request:
            # This call never held the terminal: nothing of it ran.
            return False
        try:
            try:
                orphaned = bool(self._abandon(request, stream=running.stream))
            except Exception:
                orphaned = True
            return orphaned and running.started
        finally:
            self._release(request)

    def _owned(self, request: ExecutorRequest) -> _Running:
        running = self._running
        if running is None or running.request is not request:
            raise RuntimeError("the console is not held by this command")
        return running

    def _release(self, request: ExecutorRequest) -> None:
        running = self._running
        if running is not None and running.request is request:
            self._running = None
            self._terminal.release()


__all__ = ["ConsoleExecutor"]
