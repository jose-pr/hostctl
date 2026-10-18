"""A framed command protocol for the terminal of a POSIX shell.

Nothing typed is interpreted by the terminal or the line editor: the command
and its input travel base64-encoded inside fixed shell syntax, so a ^C, TAB,
``!`` or newline in them is data. Output is framed by markers the shell
prints, each typed in two quoted halves (``printf %s%s "__ST" "<id>__"``), so
the terminal's echo of what was typed never contains one; ``stty -opost``
keeps the bytes between them exactly what the program wrote. The command's
stdin is a file or ``/dev/null``, never the terminal, and its stderr and exit
status come back in their own frames.
"""

from __future__ import annotations

import base64
import re
import time
import typing
import uuid

from ..exceptions import ConsoleProtocolError, OperationNotStartedError
from ..provider._decline import cause
from ..shell import PosixShellFlavour, ShellFlavourSelection, shell_flavour
from ._profiles import LoginStep, PromptConsoleProfile
from ._stream import ConsoleStream

#: Base64 characters per typed line. Every line stays under 1 KiB: busybox
#: ash's line editor drops a longer line (1024 bytes in its default build).
_CHUNK = 768

#: Smallest batch: one typed line (each stays under 1 KiB) must fit in it.
_MIN_BATCH = 1024

#: Typed bytes in flight before the shell acknowledges them. A serial line's
#: input queue holds 4 KiB and drops what overruns it; a macOS pty whose echo
#: nobody reads stops taking input after about 1.3 KiB (measured on sh, bash
#: and zsh), the smallest console measured.
_BATCH = _MIN_BATCH

#: Longest single write; the console's output is read between two of them.
_WRITE = 2048

#: Room kept in each batch for the acknowledgement appended to its last line.
_ACK_ROOM = 64

#: Longest single read, so a deadline is checked while the command is silent.
_POLL = 0.25

#: Reads of already-arrived output between two writes; bounds a noisy console.
_DRAIN_READS = 64

#: Pre-frame bytes kept while waiting for a marker; older ones are never needed.
_KEEP = 64 * 1024

_Data = typing.Union[str, bytes]
_Sink = typing.Optional[typing.Callable[[bytes], None]]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _halves(*parts: str) -> str:
    """A `printf` that prints `parts` joined, typed as separate quoted words."""
    return "printf " + "%s" * len(parts) + " " + " ".join(f'"{part}"' for part in parts)


def _typed_batches(
    command: bytes,
    token: str,
    *,
    input: typing.Optional[bytes] = None,
    merge_stderr: bool = False,
    batch: typing.Optional[int] = _BATCH,
) -> typing.List[typing.List[str]]:
    """The lines typed to run `command`, grouped into acknowledged batches.

    Each line is printable ASCII with no TAB, ``!`` or newline. Every batch
    but the last ends by printing ``__AK<token>_<n>__``; the last holds the
    line that runs the command. A `batch` of `None` is one batch.
    """
    var = f"__hc{token}"
    tmp = f'"${{{var}d:?}}"'

    def append(data: bytes, name: str) -> typing.List[str]:
        encoded = _b64(data)
        return [
            f"printf %s '{encoded[offset:offset + _CHUNK]}' >> {tmp}/{name}"
            for offset in range(0, len(encoded), _CHUNK)
        ]

    lines = [f"{var}d=$(mktemp -d)"] + append(command, "c")
    if input:
        lines += append(input, "i")
        # `< file`: BSD base64 takes no file operand.
        lines.append(f"base64 -d < {tmp}/i > {tmp}/f")
        stdin = f"{tmp}/f"
    else:
        stdin = "/dev/null"
    stderr = "2>&1" if merge_stderr else f"2>{tmp}/e"
    main = "; ".join(
        (
            f"{var}s=$(stty -g 2>/dev/null)",
            "stty -opost 2>/dev/null",
            _halves("__ST", f"{token}__"),
            f'eval "$(base64 -d < {tmp}/c)" < {stdin} {stderr}',
            f"{var}r=$?",
            _halves("__OUT", f"{token}__"),
            f"cat {tmp}/e 2>/dev/null",
            f"rm -rf {tmp}",
            f'stty "${var}s" 2>/dev/null',
            _halves("__EN", f"{token}_", f"${var}r", "_EN__"),
            f"unset {var}d {var}r {var}s",
        )
    )
    if batch is None:
        return [lines + [main]]
    budget = batch - _ACK_ROOM
    batches: typing.List[typing.List[str]] = [[]]
    size = 0
    for line in lines:
        if batches[-1] and size + len(line) + 1 > budget:
            batches.append([])
            size = 0
        batches[-1].append(line)
        size += len(line) + 1
    if batches[-1] and size + len(main) + 1 > budget:
        batches.append([])
    batches[-1].append(main)
    for number, batch in enumerate(batches[:-1], 1):
        batch[-1] += "; " + _halves("__AK", f"{token}_{number}__")
    return batches


def _partial(data: bytearray, marker: bytes, start: int) -> int:
    """Length of the longest suffix of ``data[start:]`` that begins `marker`."""
    for size in range(min(len(marker) - 1, len(data) - start), 0, -1):
        if data.endswith(marker[:size]):
            return size
    return 0


class _Frame:
    """Incremental parser of one command's frames on the shared stream.

    ``__ST<id>__`` opens stdout, ``__OUT<id>__`` closes it and opens stderr,
    ``__EN<id>_<status>_EN__`` ends the command. Each marker is searched only
    in bytes not scanned yet, so the cost is linear in the output.
    """

    def __init__(self, token: str) -> None:
        self.start = f"__ST{token}__".encode()
        self.mid = f"__OUT{token}__".encode()
        self.end = re.compile(rb"__EN" + token.encode() + rb"_(\d+)_EN__")
        self._end_prefix = b"__EN" + token.encode() + b"_"
        self.buffer = bytearray()
        self.state = "wait"
        self.pos = 0
        self.scan = 0
        self.out = bytearray()
        self.err = bytearray()
        self.returncode: typing.Optional[int] = None

    def feed(self, data: bytes) -> typing.Tuple[bytes, bytes]:
        """Add `data`; return the ``(stdout, stderr)`` bytes now safe to emit."""
        self.buffer += data
        out_emit = bytearray()
        err_emit = bytearray()
        while True:
            if self.state == "wait":
                hit = self.buffer.find(self.start, self.scan)
                if hit < 0:
                    # Echo, prompts and banners before the frame: keep only
                    # what could still be the start of the marker.
                    keep = len(self.start) - 1
                    if len(self.buffer) > keep:
                        del self.buffer[: len(self.buffer) - keep]
                    self.scan = 0
                    break
                self.state, self.pos = "out", hit + len(self.start)
                self.scan = self.pos
            elif self.state == "out":
                hit = self.buffer.find(self.mid, self.scan)
                if hit < 0:
                    safe = len(self.buffer) - _partial(self.buffer, self.mid, self.pos)
                    out_emit += self.buffer[self.pos : safe]
                    self.pos = self.scan = safe
                    break
                out_emit += self.buffer[self.pos : hit]
                self.state, self.pos = "err", hit + len(self.mid)
                self.scan = self.pos
            elif self.state == "err":
                match = self.end.search(self.buffer, self.scan)
                if match is None:
                    tail = self.buffer.rfind(self._end_prefix, self.pos)
                    safe = (
                        tail
                        if tail >= 0
                        else len(self.buffer)
                        - _partial(self.buffer, self._end_prefix, self.pos)
                    )
                    err_emit += self.buffer[self.pos : safe]
                    self.pos = self.scan = safe
                    break
                err_emit += self.buffer[self.pos : match.start()]
                self.returncode = int(match.group(1))
                self.state = "done"
                break
            else:
                break
        if self.pos > _KEEP:
            del self.buffer[: self.pos]
            self.scan -= self.pos
            self.pos = 0
        self.out += out_emit
        self.err += err_emit
        return bytes(out_emit), bytes(err_emit)


class PosixConsoleProfile:
    """Framed commands on the terminal of a POSIX shell.

    Needs `sh`-compatible syntax (sh, dash, bash, zsh, busybox ash, ksh) and
    `base64`, `mktemp`, `stty`, `cat` and `rm` on the target. The command runs
    in the console's own shell through `eval`, so `cd` and variables persist
    between commands and `exit` ends the session.

    `shell` names that shell's flavour (`"posix"`, `"bash"`, `"zsh"` or a
    `PosixShellFlavour`); a host driving this profile quotes structured
    commands and embeds `cwd`/`env` with it. Any other flavour raises
    `ValueError`, since the framing itself is sh syntax.

    `batch` is how many typed bytes may be in flight before the shell
    acknowledges them (at least 1024, the default; `None`: no
    acknowledgements). Keep the default on a serial line, whose 4 KiB input
    queue drops what overflows it, and on a pty read and written from one
    thread: a macOS pty stops taking input after about 1.3 KiB while its
    echo goes unread (a Linux one takes 8 and 16 KiB). A stream that blocks
    the writer and whose far end keeps reading the terminal (a web shell, an
    SSH or container session) takes `None`, saving a round trip per KiB.
    """

    can_run = True
    line_terminator = b"\n"
    encoding = "utf-8"

    def __init__(
        self,
        *,
        login: typing.Iterable[
            typing.Union[
                LoginStep,
                typing.Tuple[typing.Union[bytes, str], typing.Union[bytes, str]],
            ]
        ] = (),
        login_timeout: float = 10.0,
        wakeup: typing.Union[bytes, str] = b"\n",
        read_size: int = 4096,
        shell: ShellFlavourSelection = "posix",
        batch: typing.Optional[int] = _BATCH,
    ) -> None:
        if not login_timeout > 0:
            raise ValueError("login_timeout must be positive")
        if batch is not None and (
            isinstance(batch, bool) or not isinstance(batch, int) or batch < _MIN_BATCH
        ):
            raise ValueError(
                f"batch must be None or an int of at least {_MIN_BATCH} bytes, "
                f"got {batch!r}"
            )
        #: Typed bytes sent before waiting for the shell to acknowledge them;
        #: `None` types everything at once.
        self.batch = batch
        flavour = shell_flavour(shell)
        if not isinstance(flavour, PosixShellFlavour):
            raise ValueError(
                f"PosixConsoleProfile frames commands in sh syntax; {flavour.name} "
                "is not an sh-family shell flavour"
            )
        #: The grammar of the console's shell: argv quoting, `cd` and `export`.
        self.shell_flavour: PosixShellFlavour = flavour
        #: Seconds each login step, and the shell's first framed answer, may take.
        self.login_timeout = float(login_timeout)
        self.login = tuple(
            step if isinstance(step, LoginStep) else LoginStep(step[0], step[1])
            for step in login
        )
        self._login_patterns = tuple(
            PromptConsoleProfile._compile(step.expect) for step in self.login
        )
        self.wakeup = wakeup.encode() if isinstance(wakeup, str) else bytes(wakeup)
        self.read_size = max(1, int(read_size))

    def redact(self, transcript: bytes) -> bytes:
        """`transcript` with every `secret=True` login value replaced."""
        for step in self.login:
            if step.secret and step.send:
                transcript = transcript.replace(step.send, b"<redacted>")
        return transcript

    def negotiate(self, stream: ConsoleStream) -> None:
        """Log in, then prove a POSIX shell answers a framed command.

        Raises `TimeoutError` when a login step or the shell does not answer
        within `login_timeout`, and `ConsoleProtocolError` when the shell
        answers but the probe fails.
        """
        reset = getattr(stream, "reset_input_buffer", None)
        if callable(reset):
            try:
                reset()
            except NotImplementedError:
                pass
        if self.wakeup:
            stream.write(self.wakeup)
        transcript = b""
        for step, pattern in zip(self.login, self._login_patterns):
            transcript = self._expect(stream, pattern, transcript)
            stream.write(step.send + self.line_terminator)
        _, _, status = self.execute(stream, ":", timeout=self.login_timeout)
        if status != 0:
            raise ConsoleProtocolError(
                f"console shell answered the framed probe with status {status}"
            )

    def _expect(
        self, stream: ConsoleStream, pattern: typing.Pattern[bytes], initial: bytes
    ) -> bytes:
        """Read until `pattern` matches; return what followed the match."""
        deadline = time.monotonic() + self.login_timeout
        buffer = bytearray(initial)
        while True:
            match = pattern.search(buffer)
            if match:
                return bytes(buffer[match.end() :])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = TimeoutError("console login prompt timed out")
                error.output = self.redact(bytes(buffer))  # type: ignore[attr-defined]
                raise error
            chunk = stream.read(self.read_size, timeout=min(_POLL, remaining))
            if chunk:
                buffer += chunk
                del buffer[:-_KEEP]
            else:
                time.sleep(0.001)

    def send(self, stream: ConsoleStream, command: _Data) -> None:
        """Type `command` and a newline, for an interactive session."""
        data = command.encode(self.encoding) if isinstance(command, str) else command
        stream.write(bytes(data) + self.line_terminator)

    def run(
        self,
        stream: ConsoleStream,
        command: _Data,
        *,
        timeout: typing.Optional[float] = None,
    ) -> typing.Tuple[bytes, int]:
        """Run `command`; return its stdout and stderr merged, and its status."""
        output, _, status = self.execute(
            stream, command, merge_stderr=True, timeout=timeout
        )
        return output, status

    def execute(
        self,
        stream: ConsoleStream,
        command: _Data,
        *,
        input: typing.Optional[_Data] = None,
        merge_stderr: bool = False,
        timeout: typing.Optional[float] = None,
        on_stdout: _Sink = None,
        on_stderr: _Sink = None,
    ) -> typing.Tuple[bytes, bytes, int]:
        """Run `command`; return ``(stdout, stderr, status)`` as the program wrote them.

        `input` is the command's whole stdin; without it stdin is `/dev/null`.
        `merge_stderr` sends stderr into stdout (``2>&1``). `on_stdout`
        receives stdout as it arrives, `on_stderr` stderr when the command
        ends. On `timeout` a ^C is typed and `TimeoutError` raised, carrying
        `.output` and `.stderr` so far. A stream failure before the command
        line was typed raises `OperationNotStartedError`.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        frame, out, err = self._start(
            stream, command, input=input, merge_stderr=merge_stderr, deadline=deadline
        )
        self._emit(on_stdout, out)
        self._emit(on_stderr, err)
        while frame.state != "done":
            chunk = self._read(stream, deadline, frame)
            out, err = frame.feed(chunk)
            self._emit(on_stdout, out)
            self._emit(on_stderr, err)
        return bytes(frame.out), bytes(frame.err), typing.cast(int, frame.returncode)

    def _start(
        self,
        stream: ConsoleStream,
        command: _Data,
        *,
        input: typing.Optional[_Data],
        merge_stderr: bool,
        deadline: typing.Optional[float],
    ) -> typing.Tuple[_Frame, bytes, bytes]:
        """Type `command` through the line that runs it.

        Returns the frame parser and the ``(stdout, stderr)`` it already
        framed. Raises what `_prepare` raises; a failure while typing the
        command line itself is not a decline, since part of it may be typed.
        """
        frame, leftover, line = self._prepare(
            stream, command, input=input, merge_stderr=merge_stderr, deadline=deadline
        )
        stream.write(line)
        out, err = frame.feed(leftover)
        return frame, out, err

    def _prepare(
        self,
        stream: ConsoleStream,
        command: _Data,
        *,
        input: typing.Optional[_Data],
        merge_stderr: bool,
        deadline: typing.Optional[float],
    ) -> typing.Tuple[_Frame, bytes, bytes]:
        """Type everything `command` needs before the line that runs it.

        Returns the frame parser, what the console sent after the last
        acknowledgement, and the command line still to write. Raises
        `OperationNotStartedError` when the stream fails, and `TimeoutError`
        (after a ^C) when `deadline` passes waiting for an acknowledgement:
        either way the command line was never typed.
        """
        encoded = (
            command.encode(self.encoding)
            if isinstance(command, str)
            else bytes(command)
        )
        data = (
            None
            if input is None
            else (
                input.encode(self.encoding) if isinstance(input, str) else bytes(input)
            )
        )
        token = uuid.uuid4().hex[:16]
        batches = _typed_batches(
            encoded, token, input=data, merge_stderr=merge_stderr, batch=self.batch
        )
        frame = _Frame(token)
        leftover = b""
        for number, batch in enumerate(batches[:-1], 1):
            self._type(stream, batch)
            leftover = self._acknowledged(
                stream, f"__AK{token}_{number}__".encode(), deadline, frame
            )
        *setup, main = batches[-1]
        self._type(stream, setup, drain_after=True)
        return frame, leftover, main.encode("ascii") + b"\n"

    @staticmethod
    def _emit(sink: _Sink, data: bytes) -> None:
        if sink is not None and data:
            sink(data)

    def _type(
        self,
        stream: ConsoleStream,
        lines: typing.Sequence[str],
        *,
        drain_after: bool = False,
    ) -> None:
        """Write lines that come before the command line; a failure ran nothing.

        At most `_WRITE` bytes go in one write, and what the console sent is
        read before the next (and after the last, with `drain_after`): a
        terminal whose echo nobody reads stops reading its input, and the
        blocked write would never return. Until the command line is typed,
        that output is echo and prompts only.
        """
        pieces: typing.List[bytes] = []
        for line in lines:
            data = line.encode("ascii") + b"\n"
            if pieces and len(pieces[-1]) + len(data) <= _WRITE:
                pieces[-1] += data
            else:
                pieces.append(data)
        for index, piece in enumerate(pieces):
            if index:
                self._drain(stream)
            try:
                stream.write(piece)
            except Exception as exc:
                raise OperationNotStartedError(
                    "console write failed before the command was typed: "
                    f"{cause(exc)}",
                    cause=exc,
                ) from exc
        if pieces and drain_after:
            self._drain(stream)

    def _drain(self, stream: ConsoleStream) -> None:
        """Read and drop what the console has already sent, without waiting.

        Raises `OperationNotStartedError` when the stream has failed: it is
        only called before the command line is typed.
        """
        try:
            for _ in range(_DRAIN_READS):
                if not stream.read(self.read_size, timeout=0):
                    return
        except Exception as exc:
            raise OperationNotStartedError(
                "console stream failed before the command was typed: " f"{cause(exc)}",
                cause=exc,
            ) from exc

    def _acknowledged(
        self,
        stream: ConsoleStream,
        marker: bytes,
        deadline: typing.Optional[float],
        frame: _Frame,
    ) -> bytes:
        """Read until the shell prints `marker`; return what followed it."""
        buffer = bytearray()
        while True:
            hit = buffer.find(marker)
            if hit >= 0:
                return bytes(buffer[hit + len(marker) :])
            del buffer[: max(0, len(buffer) - len(marker) + 1)]
            try:
                chunk = self._read(stream, deadline, frame)
            except ConnectionError as exc:
                if isinstance(exc, OperationNotStartedError):
                    raise
                raise OperationNotStartedError(
                    "console stream failed before the command was typed: "
                    f"{cause(exc)}",
                    cause=exc,
                ) from exc
            buffer += chunk

    def _read(
        self, stream: ConsoleStream, deadline: typing.Optional[float], frame: _Frame
    ) -> bytes:
        """One bounded read; past the deadline, interrupt and raise."""
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                try:
                    stream.write(b"\x03")
                except Exception:
                    pass
                error = TimeoutError("console command timed out")
                error.output = bytes(frame.out)  # type: ignore[attr-defined]
                error.stderr = bytes(frame.err)  # type: ignore[attr-defined]
                raise error
            wait = _POLL if remaining is None else min(_POLL, remaining)
            chunk = stream.read(self.read_size, timeout=wait)
            if chunk:
                return chunk
            time.sleep(0.001)


__all__ = ["PosixConsoleProfile"]
