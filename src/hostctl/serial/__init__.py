"""Console protocols for serial transports.

The wire is deliberately kept separate from shell and operating-system
assumptions.  Profiles negotiate a console, optionally frame commands, and
never claim filesystem or process semantics which the device cannot provide.
"""

from __future__ import annotations

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

    def _window(self, buffer: bytes, skip: bytes | None, floor: int) -> int:
        """Where this exchange's own output can start.

        Everything before it is the command this profile just echoed back, or
        an earlier page boundary -- text that must never terminate, abort or
        forge the read. `find` is recomputed each pass because paging edits
        the buffer underneath.
        """
        if not skip:
            return floor
        index = buffer.find(skip, floor)
        return floor if index < 0 else index + len(skip)

    def _read_until(
        self,
        process: SerialProcess,
        expression: typing.Pattern[bytes],
        *,
        timeout: float | None,
        initial: bytes = b"",
        paging_process: SerialProcess | None = None,
        skip: bytes | None = None,
        floor: int = 0,
    ) -> bytes:
        started = time.monotonic()
        buffer = bytearray(initial)
        pages = 0
        while True:
            start = self._window(buffer, skip, floor)
            if paging_process is not None and self._paging_prompt is not None:
                paging = self._paging_prompt.search(buffer, start)
                if paging is not None:
                    if pages >= self.max_paging_pages:
                        raise ConsoleProtocolError(
                            "serial console paging limit exceeded"
                        )
                    pages += 1
                    # ONLY the prompt. Deleting through `paging.end()` threw
                    # away the page's content as well, so a paged command
                    # returned its LAST page and reported success -- the
                    # opposite of what paging support is for.
                    del buffer[paging.start() : paging.end()]
                    paging_process.write(self.paging_continue)
                    continue
            match = expression.search(buffer, start)
            if match:
                return bytes(buffer)
            if len(buffer) > self.max_buffer:
                # NOT a silent head truncation. The transcript used to be cut
                # from the front mid-line and returned as if complete, with
                # `error_patterns` then searched only against the surviving
                # tail -- so a 120 KB `show running-config` came back as its
                # last 64 KB with returncode 0, and a failure message printed
                # early no longer set a status.
                raise ConsoleProtocolError(
                    f"serial console output exceeded max_buffer "
                    f"({self.max_buffer} bytes); raise it on the profile to "
                    "read output this long"
                )
            if timeout is not None and time.monotonic() - started >= timeout:
                error = TimeoutError("serial console prompt timed out")
                error.output = bytes(buffer)  # type: ignore[attr-defined]
                raise error
            remaining = (
                None
                if timeout is None
                else max(0.0, timeout - (time.monotonic() - started))
            )
            chunk = self._read(process, remaining)
            if chunk:
                buffer.extend(chunk)
                continue
            # pyserial timed reads return b""; avoid a busy loop while still
            # respecting a monotonic deadline.
            time.sleep(0.001)

    def _read(self, process: SerialProcess, remaining: float | None) -> bytes:
        """One backend read, bounded by what is left of the caller's budget.

        Without the bound, `run(timeout=)` was only checked *between* reads,
        so a `SerialConfig(read_timeout=None)` host -- a supported setting
        that round-trips through the URI -- blocked inside the backend
        forever instead of raising `TimeoutExpired`.
        """
        if remaining is None:
            return process.read(self.read_size)
        try:
            return process.read(self.read_size, timeout=remaining)
        except TypeError:
            # An injected process that predates the keyword; the deadline
            # check between reads is then the only bound available.
            return process.read(self.read_size)

    def drain(self, process: SerialProcess) -> None:
        """Discard whatever the previous exchange left in the stream.

        Raw serial is one merged stream with no request/response
        correlation, so bytes left by an earlier command -- a timed-out
        `run()`, or the reply to `paging_disable` that `negotiate()` never
        read -- were consumed by the NEXT `run()` and reported as its output
        with returncode 0.
        """
        reset = getattr(process, "reset_input_buffer", None)
        if callable(reset):
            try:
                reset()
            except (NotImplementedError, ConnectionError):
                pass
        # A driver-level reset does not cover bytes already read into the
        # backend object, and an injected process may have no reset at all.
        for _ in range(64):
            try:
                if not self._read(process, 0.0):
                    return
            except (TimeoutError, ConnectionError):
                return

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
            # Its reply is deliberately NOT waited for here: a device that
            # answers nothing would cost a full prompt timeout on every
            # connect. `run()` drains before it sends, which is what keeps
            # that reply out of the first command's output.

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
        """One framed exchange, in its own window.

        The window starts after this command is echoed back and ends at the
        first prompt following the completion marker. Nothing before it can
        terminate, abort or forge the exchange: a prompt-shaped or
        marker-shaped string in the echoed command or in the output belongs
        to the output, and only the marker the device appended last decides
        the status. Whatever the previous exchange left behind is discarded
        before the command is sent, and nothing is ever dropped silently.
        """
        if not self.can_run:
            raise NotImplementedError("console profile does not provide reliable run()")
        self.drain(process)
        encoded = (
            command.encode(self.encoding)
            if isinstance(command, str)
            else bytes(command)
        )
        self.send(process, command)
        skip = encoded if self.echo else None
        started = time.monotonic()
        assert self.status_marker is not None
        # The marker first: a prompt is not the end of anything until the
        # device says the command finished.
        try:
            transcript = self._read_until(
                process,
                self.status_marker,
                timeout=timeout,
                paging_process=process,
                skip=skip,
            )
        except TimeoutError as exc:
            # The one case the login re-check is for: the device dropped to a
            # login prompt instead of running the command, so no marker is
            # ever coming. Checked HERE rather than against the body, where
            # ordinary output ("last login: root") aborted a command that had
            # in fact completed.
            captured = getattr(exc, "output", b"") or b""
            if any(pattern.search(captured) for pattern in self._login_patterns):
                raise ConsoleProtocolError(
                    "serial console requested login again"
                ) from exc
            raise
        remaining = (
            None
            if timeout is None
            else max(0.0, timeout - (time.monotonic() - started))
        )
        window = self._window(transcript, skip, 0)
        last = None
        for candidate in self.status_marker.finditer(transcript, window):
            last = candidate
        assert last is not None
        transcript = self._read_until(
            process,
            self._prompt,
            timeout=remaining,
            initial=transcript,
            paging_process=process,
            floor=last.end(),
        )
        window = self._window(transcript, skip, 0)
        marker = None
        for candidate in self.status_marker.finditer(transcript, window):
            marker = candidate
        if marker is None:
            raise ConsoleProtocolError("command completion marker missing")
        body = transcript[window : marker.start()]
        # Whatever terminal editing left of the echoed line.
        if self.echo and body.startswith(encoded):
            body = body[len(encoded) :]
        body = body.lstrip(b"\r\n")
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
