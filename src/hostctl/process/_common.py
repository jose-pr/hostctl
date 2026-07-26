"""Contracts for persistent child processes and terminal allocation."""

from __future__ import annotations

import dataclasses
import typing
import types
import codecs

ProcessData = typing.Union[str, bytes]


def raise_normalized(
    exc: Exception, normalizer: typing.Callable[[Exception], Exception]
) -> typing.NoReturn:
    """Raise a normalized transport error while preserving its cause."""
    normalized = normalizer(exc)
    if normalized is exc:
        raise exc
    raise normalized from exc


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
    """Synchronous control surface for a persistent child process."""

    @property
    def returncode(self) -> typing.Optional[int]: ...

    def write(self, data: ProcessData) -> None: ...

    def read(self, size: int = -1) -> ProcessData: ...

    def read_stderr(self, size: int = -1) -> ProcessData: ...

    def send_eof(self) -> None: ...

    def resize(
        self,
        columns: int,
        rows: int,
        pixel_width: int = 0,
        pixel_height: int = 0,
    ) -> None: ...

    def wait(self, timeout: typing.Optional[float] = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def close(self) -> None: ...

    def __enter__(self) -> Process: ...

    def __exit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[types.TracebackType],
    ) -> bool: ...
