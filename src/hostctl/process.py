"""Contracts for persistent child processes and terminal allocation."""

from __future__ import annotations

import dataclasses
import threading
import typing
import types
import codecs

from .executor import LeaseToken
from .executor import expired
from .executor import raise_normalized as raise_normalized

ProcessData = typing.Union[str, bytes]

#: What `threading.RLock()` returns. `threading.RLock` itself is a
#: FACTORY FUNCTION, not a class, so it cannot annotate anything --
#: a re-entrant lock's only portable spelling is the protocol it
#: satisfies.
ReentrantLock = typing.ContextManager[bool]


class IncrementalTextDecoder:
    """Decode arbitrary byte chunks without splitting multibyte characters."""

    def __init__(self, encoding: str, errors: str = "strict") -> None:
        self._decoder = codecs.getincrementaldecoder(encoding)(errors)

    def decode(self, data: bytes, *, final: bool = False) -> str:
        return self._decoder.decode(data, final=final)


@dataclasses.dataclass(frozen=True)
class TerminalOptions:
    """Requested pseudo-terminal type and initial dimensions."""

    term_type: str = "xterm-256color"
    columns: int = 80
    rows: int = 24
    pixel_width: int = 0
    pixel_height: int = 0

    def __post_init__(self) -> None:
        if not self.term_type:
            raise ValueError("term_type must not be empty")
        if self.columns <= 0 or self.rows <= 0:
            raise ValueError("terminal columns and rows must be positive")
        if self.pixel_width < 0 or self.pixel_height < 0:
            raise ValueError("terminal pixel dimensions must not be negative")

    @property
    def size(self) -> typing.Tuple[int, int, int, int]:
        return (
            self.columns,
            self.rows,
            self.pixel_width,
            self.pixel_height,
        )


TerminalRequest = typing.Optional[typing.Union[bool, TerminalOptions]]


def terminal_options(value: TerminalRequest) -> typing.Optional[TerminalOptions]:
    """Normalize a convenient boolean terminal request to concrete options."""
    if value is True:
        return TerminalOptions()
    if value in (False, None):
        return None
    if not isinstance(value, TerminalOptions):
        raise TypeError("terminal must be bool, TerminalOptions, or None")
    return value


@typing.runtime_checkable
class Process(typing.Protocol):
    """Synchronous control surface for a persistent child process.

    Every member raises rather than carrying the conventional `...` body.
    Protocol members are NOT `@abstractmethod`, so five concrete adapters
    inherit this class explicitly and an inherited-but-unimplemented member
    was a real method returning `None`: instantiation succeeded,
    `isinstance(x, Process)` passed, and `process.wait(60) == 0` failed as
    `assert None == 0` -- or, in `if process.wait():`, read as "exited 0".
    The conformance battery exists to catch exactly "advertised but not
    implemented", and this base class was converting that mistake into a
    silent wrong value.

    Structural typing is unaffected: a class that satisfies the protocol
    without inheriting it never reaches these bodies.
    """

    @property
    def returncode(self) -> typing.Optional[int]:
        # The one member that keeps a `...` body. On the 3.9 floor,
        # `isinstance(x, Process)` is implemented with `hasattr`, which CALLS
        # a property -- so raising here made the runtime check itself raise
        # `NotImplementedError` instead of answering True or False. 3.11+
        # looks at the class and never evaluates it. `returncode` is also the
        # one member every adapter implements; the methods below are where a
        # gap actually went unnoticed.
        ...

    def write(self, data: ProcessData) -> None:
        raise NotImplementedError(f"{type(self).__name__}.write")

    def read(self, size: int = -1) -> ProcessData:
        raise NotImplementedError(f"{type(self).__name__}.read")

    def read_stderr(self, size: int = -1) -> ProcessData:
        raise NotImplementedError(f"{type(self).__name__}.read_stderr")

    def send_eof(self) -> None:
        raise NotImplementedError(f"{type(self).__name__}.send_eof")

    def resize(
        self,
        columns: int,
        rows: int,
        pixel_width: int = 0,
        pixel_height: int = 0,
    ) -> None:
        raise NotImplementedError(f"{type(self).__name__}.resize")

    def wait(self, timeout: typing.Optional[float] = None) -> int:
        raise NotImplementedError(f"{type(self).__name__}.wait")

    def terminate(self) -> None:
        raise NotImplementedError(f"{type(self).__name__}.terminate")

    def kill(self) -> None:
        raise NotImplementedError(f"{type(self).__name__}.kill")

    def close(self) -> None:
        raise NotImplementedError(f"{type(self).__name__}.close")

    def __enter__(self) -> Process:
        raise NotImplementedError(f"{type(self).__name__}.__enter__")

    def __exit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[types.TracebackType],
    ) -> bool:
        raise NotImplementedError(f"{type(self).__name__}.__exit__")


class _LeasedStreamProcess(Process):
    """A merged byte stream that holds one :class:`StreamLease` acquisition.

    Subclasses give the transport's I/O and its message texts, and put
    anything that must run once at close in `_teardown()`. The lease ending
    -- through `close()` or revoked by its owner -- is the process's "closed"
    state: `returncode` is then 0, `wait()` returns and I/O raises
    `ConnectionError`.
    """

    _closed_message = "process is closed"
    _wait_label = "stream"
    _stderr_message = "the stream is merged"
    _eof_message = "the stream cannot be half-closed"
    _terminate_message = "the stream has no terminate signal"
    _kill_message = "the stream has no kill signal"

    def __init__(
        self,
        *,
        io_lock: typing.Optional[ReentrantLock] = None,
        token: typing.Optional[LeaseToken] = None,
        release: typing.Callable[[], None],
    ) -> None:
        self._io_lock = io_lock or threading.RLock()
        # A process built without a lease (a test double, a one-off stream)
        # carries a token of its own, so `close()` still ends it.
        token = token or LeaseToken()
        self._token = token
        self._release = release
        self._closed = token.ended
        # Decides who tears down; never held across I/O.
        self._claim_lock = threading.Lock()
        self._claimed = False

    @property
    def returncode(self) -> typing.Optional[int]:
        return 0 if self._closed.is_set() else None

    def _require_open(self) -> None:
        if self._closed.is_set():
            raise ConnectionError(self._closed_message)

    def read_stderr(self, size: int = -1) -> bytes:
        raise NotImplementedError(self._stderr_message)

    def send_eof(self) -> None:
        raise NotImplementedError(self._eof_message)

    def terminate(self) -> None:
        raise NotImplementedError(self._terminate_message)

    def kill(self) -> None:
        raise NotImplementedError(self._kill_message)

    def wait(self, timeout: typing.Optional[float] = None) -> int:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must not be negative")
        if not self._closed.wait(timeout):
            raise expired(self._wait_label, timeout, orphaned=True)
        return 0

    def _teardown(self) -> None:
        """Run once, by the one close() that wins; may raise after release."""

    def close(self) -> None:
        # Check-then-act on `_closed` let a concurrent close() and an
        # EOF-driven close both run, releasing the lease twice. One winner
        # claims the teardown.
        with self._claim_lock:
            if self._closed.is_set() or self._claimed:
                return
            self._claimed = True
        try:
            self._teardown()
        finally:
            self._closed.set()
            self._release()

    def __enter__(self):
        return self

    def __exit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[types.TracebackType],
    ) -> bool:
        self.close()
        return False
