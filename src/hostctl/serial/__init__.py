"""Console protocols for serial transports.

The wire is deliberately kept separate from shell and operating-system
assumptions.  Profiles negotiate a console, optionally frame commands, and
never claim filesystem or process semantics which the device cannot provide.
"""

from __future__ import annotations

import abc
import dataclasses
import re
import time
import typing

from ..process.serial import SerialProcess


@typing.runtime_checkable
class SerialConsoleProtocol(typing.Protocol):
    """Runtime contract implemented by serial console profiles."""

    can_run: bool
    line_terminator: bytes
    encoding: str

    def negotiate(self, process: SerialProcess) -> None: ...

    def send(self, process: SerialProcess, command: str | bytes) -> None: ...

    def run(
        self,
        process: SerialProcess,
        command: str | bytes,
        *,
        timeout: float | None = None,
    ) -> tuple[bytes, int]: ...


class ConsoleProtocolError(ConnectionError):
    """The console did not complete the expected protocol exchange."""


@dataclasses.dataclass(frozen=True, repr=False)
class LoginStep:
    """One bounded expect/send step used by :class:`PromptConsoleProfile`."""

    expect: bytes | str
    send: bytes | str
    secret: bool = False

    def __post_init__(self) -> None:
        expect = self.expect.encode() if isinstance(self.expect, str) else self.expect
        send = self.send.encode() if isinstance(self.send, str) else self.send
        if not expect:
            raise ValueError("login expect expression must not be empty")
        object.__setattr__(self, "expect", bytes(expect))
        object.__setattr__(self, "send", bytes(send))

    def __repr__(self) -> str:
        value = "<redacted>" if self.secret else repr(self.send)
        return (
            f"LoginStep(expect={self.expect!r}, send={value}, secret={self.secret!r})"
        )


class RawConsoleProfile:
    """Raw byte console; it supports interactive sessions but not ``run``."""

    can_run = False
    line_terminator = b"\r\n"
    encoding = "utf-8"
    max_buffer = 64 * 1024

    def negotiate(self, process: SerialProcess) -> None:
        return None

    def send(self, process: SerialProcess, command: str | bytes) -> None:
        data = (
            command.encode(self.encoding)
            if isinstance(command, str)
            else bytes(command)
        )
        process.write(data + self.line_terminator)

    def run(self, process: SerialProcess, command: str | bytes, *, timeout=None):
        raise NotImplementedError("raw serial consoles do not provide framed run()")


class PromptConsoleProfile:
    """Configurable prompt/login framing for terminal-like devices.

    A profile only advertises ``run`` when ``reliable_status`` is explicitly
    enabled.  Without a real status marker a prompt can delimit output, but it
    cannot truthfully represent a process exit status.
    """

    def __init__(
        self,
        prompt: bytes | str,
        *,
        login: typing.Iterable[LoginStep | tuple[bytes | str, bytes | str]] = (),
        line_terminator: bytes | str = b"\r\n",
        error_patterns: typing.Iterable[bytes | str] = (),
        status_marker: bytes | str | None = None,
        reliable_status: bool = False,
        max_buffer: int = 64 * 1024,
        read_size: int = 4096,
        wakeup: bytes = b"\r",
        echo: bool = True,
        paging_prompt: bytes | str | None = None,
        paging_continue: bytes | str = b" ",
        paging_disable: bytes | str | None = None,
        max_paging_pages: int = 32,
        terminal_setup: typing.Callable[[SerialProcess, int, int], None] | None = None,
        status_parser: typing.Callable[[re.Match[bytes]], int] | None = None,
    ) -> None:
        self._prompt = self._compile(prompt)
        self.login = tuple(
            step if isinstance(step, LoginStep) else LoginStep(step[0], step[1])
            for step in login
        )
        self._login_patterns = tuple(self._compile(step.expect) for step in self.login)
        self.line_terminator = (
            line_terminator.encode()
            if isinstance(line_terminator, str)
            else bytes(line_terminator)
        )
        if not self.line_terminator:
            raise ValueError("line_terminator must not be empty")
        self.error_patterns = tuple(self._compile(value) for value in error_patterns)
        self.status_marker = (
            self._compile(status_marker) if status_marker is not None else None
        )
        self.reliable_status = bool(reliable_status)
        self.can_run = self.reliable_status and self.status_marker is not None
        self.max_buffer = max(1024, int(max_buffer))
        self.read_size = max(1, int(read_size))
        self.wakeup = bytes(wakeup)
        self.echo = bool(echo)
        self._paging_prompt = self._compile(paging_prompt) if paging_prompt else None
        self.paging_continue = (
            paging_continue.encode()
            if isinstance(paging_continue, str)
            else bytes(paging_continue)
        )
        self.paging_disable = (
            paging_disable.encode()
            if isinstance(paging_disable, str)
            else (bytes(paging_disable) if paging_disable is not None else None)
        )
        if max_paging_pages < 0:
            raise ValueError("max_paging_pages must not be negative")
        self.max_paging_pages = int(max_paging_pages)
        self.terminal_setup = terminal_setup
        self.status_parser = status_parser
        self.encoding = "utf-8"

    @staticmethod
    def _compile(value: bytes | str) -> typing.Pattern[bytes]:
        raw = value.encode() if isinstance(value, str) else bytes(value)
        if not raw:
            raise ValueError("console expressions must not be empty")
        try:
            return re.compile(raw)
        except re.error as exc:
            raise ValueError(f"invalid console expression: {exc}") from exc

    def _read_until(
        self,
        process: SerialProcess,
        expression: typing.Pattern[bytes],
        *,
        timeout: float | None,
        initial: bytes = b"",
        paging_process: SerialProcess | None = None,
    ) -> bytes:
        started = time.monotonic()
        buffer = bytearray(initial)
        pages = 0
        while True:
            if paging_process is not None and self._paging_prompt is not None:
                paging = self._paging_prompt.search(buffer)
                if paging is not None:
                    if pages >= self.max_paging_pages:
                        raise ConsoleProtocolError(
                            "serial console paging limit exceeded"
                        )
                    pages += 1
                    del buffer[: paging.end()]
                    paging_process.write(self.paging_continue)
                    continue
            match = expression.search(buffer)
            if match:
                return bytes(buffer)
            if len(buffer) > self.max_buffer:
                del buffer[: -self.max_buffer]
            if timeout is not None and time.monotonic() - started >= timeout:
                error = TimeoutError("serial console prompt timed out")
                error.output = bytes(buffer)  # type: ignore[attr-defined]
                raise error
            chunk = process.read(self.read_size)
            if chunk:
                buffer.extend(chunk)
                continue
            # pyserial timed reads return b""; avoid a busy loop while still
            # respecting a monotonic deadline.
            time.sleep(0.001)

    def negotiate(self, process: SerialProcess) -> None:
        transcript = b""
        reset = getattr(process, "reset_input_buffer", None)
        if callable(reset):
            try:
                reset()
            except NotImplementedError:
                pass
        if self.wakeup:
            process.write(self.wakeup)
        for step in self.login:
            transcript = self._read_until(
                process, self._compile(step.expect), timeout=10, initial=transcript
            )
            process.write(step.send + self.line_terminator)
        self._read_until(process, self._prompt, timeout=10, initial=transcript)
        if self.paging_disable is not None:
            process.write(self.paging_disable + self.line_terminator)

    def resize(self, process: SerialProcess, columns: int, rows: int) -> None:
        if self.terminal_setup is None:
            raise NotImplementedError("console profile does not support terminal setup")
        if columns <= 0 or rows <= 0:
            raise ValueError("terminal columns and rows must be positive")
        self.terminal_setup(process, columns, rows)

    def send(self, process: SerialProcess, command: str | bytes) -> None:
        data = (
            command.encode(self.encoding)
            if isinstance(command, str)
            else bytes(command)
        )
        process.write(data + self.line_terminator)

    def run(self, process: SerialProcess, command: str | bytes, *, timeout=None):
        if not self.can_run:
            raise NotImplementedError("console profile does not provide reliable run()")
        self.send(process, command)
        transcript = self._read_until(
            process, self._prompt, timeout=timeout, paging_process=process
        )
        if any(pattern.search(transcript) for pattern in self._login_patterns):
            raise ConsoleProtocolError("serial console requested login again")
        marker = self.status_marker.search(transcript) if self.status_marker else None
        if marker is None:
            raise ConsoleProtocolError("command completion marker missing")
        body = transcript[: marker.start()]
        # Remove a simple echoed command without attempting to parse vendor
        # terminal editing rules.
        if self.echo:
            encoded = (
                command.encode(self.encoding)
                if isinstance(command, str)
                else bytes(command)
            )
            if body.startswith(encoded):
                body = body[len(encoded) :].lstrip(b"\r\n")
        status = (
            self.status_parser(marker)
            if self.status_parser
            else (
                1 if any(pattern.search(body) for pattern in self.error_patterns) else 0
            )
        )
        if not isinstance(status, int) or status < 0:
            raise ConsoleProtocolError("console status parser returned an invalid code")
        return body, status


__all__ = [
    "ConsoleProtocolError",
    "LoginStep",
    "PromptConsoleProfile",
    "RawConsoleProfile",
    "SerialConsoleProtocol",
]
