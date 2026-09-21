"""Contracts for persistent child processes and terminal allocation."""

from __future__ import annotations

import dataclasses
import typing
import types
import codecs

from ..executor._common import raise_normalized as raise_normalized

ProcessData = typing.Union[str, bytes]


class IncrementalTextDecoder:
    """Decode arbitrary byte chunks without splitting multibyte characters."""

    def __init__(self, encoding: str, errors: str = "strict") -> None:
        self._decoder = codecs.getincrementaldecoder(encoding)(errors)

    def decode(self, data: bytes, *, final: bool = False) -> str:
        return self._decoder.decode(data, final=final)


TerminalRequest = typing.Optional[typing.Union[bool, "TerminalOptions"]]


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
