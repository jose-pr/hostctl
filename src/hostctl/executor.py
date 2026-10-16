"""Transport-independent command executor contracts."""

from __future__ import annotations

import contextvars
import enum
import os
import re
import subprocess
import sys
import threading
import typing
from pathlib import PurePath

from pathapi import Pathname

_Result = typing.TypeVar("_Result", covariant=True)

StreamTarget = typing.Union[int, typing.BinaryIO, typing.TextIO]
#: `bytearray` and `memoryview` are here because callers really do pass
#: them -- a `socket.recv_into` buffer, a slice of a `mmap` -- and the whole
#: point of `normalize_input` is that the sink must never see a type it
#: cannot write.
CommandInput = typing.Optional[typing.Union[bytes, bytearray, memoryview, str]]
StrPath = typing.Union[str, os.PathLike[str]]
CommandEnvironment = typing.Mapping[typing.Union[str, bytes], object]
CaptureOutput = typing.Literal[True, False, "stdout", "stderr"]


class CommandLine(str):
    """A command line already quoted for the target's own command parser.

    Most shells render as argv -- a program plus elements an exec-style API
    delivers verbatim -- and `ShellFlavour.invocation()` returns that. `cmd`
    cannot: Windows builds a child's command line with `CreateProcess`
    quoting (`subprocess.list2cmdline`), which escapes every `"` it is given,
    and cmd reads those escapes as literal data. There is provably no argv
    element whose encoded form carries an unescaped quote, so a cmd script
    containing quoting cannot survive argv delivery at all -- measured
    against a real `cmd.exe`, 14 of 16 adversarial values were corrupted.

    This marker says "submit this text as the command line, do not re-quote
    it". It subclasses `str` so a transport that already submits shell text
    (an SSH exec channel, WinRM) needs no special case: it is the text.
    """

    __slots__ = ()


CommandLike = typing.Union[str, PurePath, Pathname]
CommandArgument = typing.Union[str, bytes, PurePath, Pathname]


#: The PowerShell status every WinRM leg and the PowerShell flavour report: 0
#: when the last statement succeeded (`$?`), else the native exit code if
#: there is one, else 1.
POWERSHELL_STATUS = (
    "$(if ($?) { 0 } elseif ($LASTEXITCODE) { $LASTEXITCODE } else { 1 })"
)


def capture_streams(
    capture_output: CaptureOutput,
    stdout: typing.Optional[StreamTarget],
    stderr: typing.Optional[StreamTarget],
) -> typing.Tuple[typing.Optional[StreamTarget], typing.Optional[StreamTarget]]:
    """Apply hostctl's extended capture_output convention."""
    if capture_output not in (True, False, "stdout", "stderr"):
        raise ValueError("capture_output must be True, False, 'stdout', or 'stderr'")
    if capture_output == "stdout":
        if stdout not in (None, subprocess.PIPE):
            raise ValueError(
                "stdout argument cannot be used with capture_output='stdout'"
            )
        stdout = subprocess.PIPE
    elif capture_output == "stderr":
        if stderr not in (None, subprocess.PIPE):
            raise ValueError(
                "stderr argument cannot be used with capture_output='stderr'"
            )
        stderr = subprocess.PIPE
    elif capture_output is True:
        if stdout is None:
            stdout = subprocess.PIPE
        if stderr is None:
            stderr = subprocess.PIPE
    return stdout, stderr


def reject_stdin_conflict(
    input: CommandInput, stdin: typing.Optional[StreamTarget]
) -> None:
    if input is not None and stdin is not None:
        raise ValueError("stdin and input arguments may not both be used")


def raise_normalized(
    exc: Exception, normalizer: typing.Callable[[Exception], Exception]
) -> "typing.NoReturn":
    """Raise a normalized transport error while preserving its cause.

    Lives here rather than in `process._common` because the executors need
    it too, and `executor` is the layer `process` already imports from --
    the other direction is a cycle. Nine call sites had inlined its three
    lines verbatim, which is how two of them ended up dropping the
    `from exc`.
    """
    normalized = normalizer(exc)
    if normalized is exc:
        raise exc
    raise normalized from exc


def normalize_input(
    input: CommandInput,
    *,
    text_mode: bool,
    encoding: typing.Optional[str] = None,
    errors: typing.Optional[str] = None,
) -> CommandInput:
    """Match `input` to the stream mode the executor is about to use.

    A mismatch here is not a clean error. `subprocess` writes `input` on a
    daemon writer thread; handing `bytes` to a text-mode stdin raises
    `TypeError` *inside that thread*, which dies without closing the pipe, so
    the child never sees EOF and the call blocks forever -- `timeout=` does not
    fire, because nothing is waiting on the child. Verified on CPython 3.14;
    3.9 happens to surface the `TypeError` instead, so the failure mode is
    interpreter-dependent, which is worse than either outcome alone.

    Every executor needs this, not just the local one, and they must agree:
    a `SystemHost` can dispatch the same call through different providers on
    different attempts, so a caller cannot write one correct invocation if
    each provider normalizes differently. `text_mode` is a parameter rather
    than inferred because the sinks differ -- `subprocess` wants `str` under a
    text mode, AsyncSSH wants `bytes` when given no encoding.

    Passing `bytes` under a text mode decodes them; passing `str` under a
    binary mode encodes them. `encoding`/`errors` default to UTF-8/strict,
    matching `subprocess`.
    """
    if input is None:
        return None
    if not isinstance(input, (bytes, bytearray, memoryview, str)):
        # `subprocess` hands any other value to its writer thread, which dies
        # without closing the pipe: the call hangs until `timeout` on 3.14.
        raise TypeError(
            "input must be bytes, bytearray, memoryview or str, "
            f"not {type(input).__name__}"
        )
    if text_mode:
        if isinstance(input, (bytes, bytearray, memoryview)):
            # A `bytearray` reached the text-mode writer thread untouched
            # and raised there -- exactly the deadlock this function exists
            # to prevent, for the two buffer types it did not check.
            return bytes(input).decode(encoding or "utf-8", errors or "strict")
        return input
    if isinstance(input, str):
        return input.encode(encoding or "utf-8", errors or "strict")
    if isinstance(input, (bytearray, memoryview)):
        # AsyncSSH and the QGA base64 leg both want real `bytes`.
        return bytes(input)
    return input


def wants_text(
    text: typing.Optional[bool],
    encoding: typing.Optional[str],
    errors: typing.Optional[str],
) -> bool:
    """Decide whether one call's output is `str` rather than `bytes`.

    `subprocess.run`'s rule, and the only one hostctl has: any of `text`,
    `encoding`, or `errors` selects text mode. `text=False` does not veto an
    `encoding` -- `subprocess` does not treat it as one either.

    This exists as a shared function because the answer must not depend on
    which provider a `SystemHost` happened to select. Four executors inferred
    it as `bool(encoding or errors or text)` while SSH, PSRP, and the serial
    host each ignored `errors`, so `run(cmd, errors="replace")` returned
    `str` or `bytes` depending on which transport won the fallback.
    """
    return bool(text or encoding or errors)


def command_text(
    value: object, *, encoding: str = "utf-8", errors: str = "strict"
) -> str:
    """Render one command or argv value as the text a transport sends.

    Anything offering `__fspath__` is asked for it first: that is the
    filesystem representation a path promises, and it is what a transport
    needs. `str()` happens to match for the path types shipped here, but that
    is a coincidence of their `__str__` rather than a contract -- a path type
    whose `repr`-ish `__str__` differs would otherwise send the wrong text.

    `__fspath__` is tried directly rather than through
    `isinstance(value, os.PathLike)`, so a duck-typed path that never
    registered with the ABC is still honoured. A object whose `__fspath__`
    raises or returns a non-string falls back to `str()` rather than failing
    the command.

    Bytes -- returned by `__fspath__` or passed directly -- are decoded;
    everything else is stringified.
    """
    fspath = getattr(value, "__fspath__", None)
    if callable(fspath):
        try:
            resolved = fspath()
        except Exception:
            resolved = None
        if isinstance(resolved, (str, bytes)):
            value = resolved
    if isinstance(value, bytes):
        return value.decode(encoding, errors)
    return str(value)


def expired(
    command: object,
    timeout: typing.Optional[float],
    *,
    output: typing.Optional[typing.Union[bytes, str]] = None,
    stderr: typing.Optional[typing.Union[bytes, str]] = None,
    orphaned: bool = False,
    pid: typing.Optional[int] = None,
    text: bool = False,
) -> subprocess.TimeoutExpired:
    """Build the one `TimeoutExpired` shape every hostctl transport raises.

    Every transport sets the same attributes, `.orphaned` and `.pid`
    included, and the same payload types, so a supervisor writing
    `if exc.orphaned: alert(exc.pid)` works whichever transport timed out.

    `orphaned` is `True` when the transport could NOT stop the command, so
    it is still running somewhere; `pid` is its identifier where the
    transport knows one.
    """
    empty: typing.Union[bytes, str] = "" if text else b""
    error = subprocess.TimeoutExpired(
        command,
        timeout,
        output=empty if output is None else output,
        stderr=empty if stderr is None else stderr,
    )
    error.orphaned = bool(orphaned)  # type: ignore[attr-defined]
    error.pid = pid  # type: ignore[attr-defined]
    return error


#: A name every transport can carry: embedded `export`/`$env:`/`set` lines
#: and a native environment block alike.
ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_environment_names(env: typing.Optional[CommandEnvironment]) -> None:
    """Raise `ValueError` for an environment variable name a transport cannot carry.

    One rule, applied before a provider is selected: a name that only some
    transports could carry made the same call valid or not depending on which
    provider won.
    """
    if not env:
        return
    for key in env:
        name = key.decode("utf-8", "surrogateescape") if isinstance(key, bytes) else key
        if not isinstance(name, str) or not ENVIRONMENT_NAME.fullmatch(name):
            raise ValueError(f"invalid environment variable name: {key!r}")


def normalize_environment(
    env: typing.Optional[CommandEnvironment],
) -> typing.Optional[typing.Dict[str, str]]:
    if env is None:
        return None
    validate_environment_names(env)
    return {
        key.decode() if isinstance(key, bytes) else str(key): (
            value.decode() if isinstance(value, bytes) else str(value)
        )
        for key, value in env.items()
    }


#: What a credential is replaced with in text meant for a log or an exception.
MASK = "<redacted>"

#: The command a dispatch shows to a log or an exception, when it differs from
#: the one sent: the same rendering with each embedded environment value masked.
_DISPATCH_DISPLAY: contextvars.ContextVar[typing.Optional[typing.Tuple[str, ...]]] = (
    contextvars.ContextVar("hostctl_dispatch_display", default=None)
)


def masked_environment(
    env: CommandEnvironment,
) -> typing.Dict[typing.Union[str, bytes], str]:
    """`env` with every value replaced by `MASK`; the names stay visible."""
    return {key: MASK for key in env}


def dispatch_display() -> typing.Optional[typing.Tuple[str, ...]]:
    """The masked command of the dispatch in progress, or `None`."""
    return _DISPATCH_DISPLAY.get()


def _show(error: typing.Optional[BaseException], shown: object, seen: set) -> None:
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(
            error, (subprocess.CalledProcessError, subprocess.TimeoutExpired)
        ):
            old = error.cmd
            error.cmd = shown
            error.args = tuple(shown if item is old else item for item in error.args)
        _show(error.__cause__, shown, seen)
        error = error.__context__


def run_displayed(
    display: typing.Optional[typing.Tuple[str, ...]],
    call: typing.Callable[[], typing.Any],
) -> typing.Any:
    """Run `call`, showing `display` wherever the command it sent would appear.

    The log line, `CompletedProcess.args` and the `cmd` of a raised
    `CalledProcessError`/`TimeoutExpired` (and of the errors chained to it)
    carry the masked command, so an environment value embedded in the command
    text stays out of a traceback. The same exception object is re-raised.
    """
    if display is None:
        return call()
    shown: typing.Any = display[0] if len(display) == 1 else list(display)
    token = _DISPATCH_DISPLAY.set(display)
    try:
        result = call()
    except BaseException as exc:
        _show(exc, shown, set())
        raise
    finally:
        _DISPATCH_DISPLAY.reset(token)
    if isinstance(result, subprocess.CompletedProcess):
        result.args = shown
    return result


def write_output(
    stream: StreamTarget,
    value: typing.Optional[typing.Union[str, bytes]],
    *,
    encoding: typing.Optional[str],
    errors: typing.Optional[str],
) -> None:
    """Write completed buffered output without taking ownership of the stream."""
    if value is None or stream == subprocess.DEVNULL:
        return
    close = False
    if isinstance(stream, int):
        stream = os.fdopen(os.dup(stream), "wb", closefd=True)
        close = True
    try:
        try:
            stream.write(value)
        except TypeError:
            # A text stream (`sys.stdout`, which is what stdout=None means)
            # cannot take bytes. Prefer its binary buffer: that passes the
            # output through byte for byte, which is what the caller asked
            # for. Decoding instead used errors="strict", so a completed
            # command whose output was not valid UTF-8 died with
            # UnicodeDecodeError -- and the mirror direction died with
            # UnicodeEncodeError on a narrow console codepage.
            buffer = getattr(stream, "buffer", None)
            if buffer is not None:
                # Text already written to the wrapper is still pending in it;
                # the buffer write below would overtake it.
                pending = getattr(stream, "flush", None)
                if pending is not None:
                    pending()
            if isinstance(value, bytes):
                if buffer is not None:
                    buffer.write(value)
                else:
                    stream.write(value.decode(encoding or "utf-8", errors or "replace"))
            else:
                encoded = value.encode(encoding or "utf-8", errors or "replace")
                if buffer is not None:
                    buffer.write(encoded)
                else:
                    stream.write(encoded)
        flush = getattr(stream, "flush", None)
        if flush is not None:
            flush()
    finally:
        if close:
            stream.close()


def dispatch_output(
    stdout_target: typing.Optional[StreamTarget],
    stderr_target: typing.Optional[StreamTarget],
    stdout: typing.Optional[typing.Union[str, bytes]],
    stderr: typing.Optional[typing.Union[str, bytes]],
    *,
    encoding: typing.Optional[str],
    errors: typing.Optional[str],
) -> typing.Tuple[
    typing.Optional[typing.Union[str, bytes]],
    typing.Optional[typing.Union[str, bytes]],
]:
    if stdout_target != subprocess.PIPE:
        write_output(
            sys.stdout if stdout_target is None else stdout_target,
            stdout,
            encoding=encoding,
            errors=errors,
        )
        stdout = None
    if stderr_target not in (subprocess.PIPE, subprocess.STDOUT):
        write_output(
            sys.stderr if stderr_target is None else stderr_target,
            stderr,
            encoding=encoding,
            errors=errors,
        )
        stderr = None
    return stdout, stderr


class ExecutorCapability(str, enum.Enum):
    """Native executor features, spelled as strings.

    There is exactly one capability vocabulary in hostctl: **strings**.  These
    members subclass :class:`str` so ``ExecutorCapability.CWD == "cwd"`` and
    both hash alike -- a set of members and a set of their spellings are
    interchangeable, with no conversion step at any boundary.

    Strings are the vocabulary rather than opaque enum members because
    provider capability sets legitimately carry transport-specific tokens with
    no member here ("runspace", plus whatever a third-party provider
    declares).  An enum-only vocabulary could not express those; a mixed one
    would need coercion at every comparison, which is precisely the bug this
    shape removes.  `str, enum.Enum` (not `enum.StrEnum`) keeps the 3.9 floor.
    """

    ARGS = "args"
    CWD = "cwd"
    ENV = "env"
    SCRIPT = "script"
    MANAGES_STATUS = "manages_status"


class ExecutionOptions(typing.TypedDict, total=False):
    """Shell-agnostic subprocess-style options understood by executors."""

    stdin: typing.Optional[StreamTarget]
    stdout: typing.Optional[StreamTarget]
    stderr: typing.Optional[StreamTarget]
    cwd: typing.Optional[StrPath]
    env: typing.Optional[CommandEnvironment]
    capture_output: CaptureOutput
    check: bool
    encoding: typing.Optional[str]
    errors: typing.Optional[str]
    input: CommandInput
    timeout: typing.Optional[float]
    text: bool


@typing.runtime_checkable
class Executor(typing.Protocol[_Result]):
    """Callable executor receiving one command string and execution options."""

    #: Declared as strings: `ExecutorCapability` members subclass `str` and
    #: stay the documented spelling, but the vocabulary itself is open --
    #: a third-party executor may declare a capability this enum does not
    #: know, and the narrower annotation called that a type error.
    executor_capabilities: typing.FrozenSet[str]

    def __call__(
        self,
        command: CommandLike,
        *args: CommandArgument,
        stdin: typing.Optional[StreamTarget] = None,
        stdout: typing.Optional[StreamTarget] = None,
        stderr: typing.Optional[StreamTarget] = None,
        cwd: typing.Optional[StrPath] = None,
        env: typing.Optional[CommandEnvironment] = None,
        capture_output: typing.Optional[CaptureOutput] = None,
        check: typing.Optional[bool] = None,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
        input: CommandInput = None,
        timeout: typing.Optional[float] = None,
        text: typing.Optional[bool] = None,
        **options: object,
    ) -> _Result: ...


class LeaseToken:
    """One acquisition of a :class:`StreamLease`; ``ended`` is set when it ends."""

    __slots__ = ("ended",)

    def __init__(self) -> None:
        self.ended = threading.Event()


class StreamLease:
    """An exclusive lease whose release is bound to the acquisition it frees.

    A bare lock is released by whoever calls `release()`, so a stale handle
    closing late freed the lease of the process that replaced it. Here only
    the current holder's token can free it; `revoke()` ends the holder from
    outside, and `on_end` runs once whenever a holder ends.
    """

    def __init__(
        self,
        busy_message: str,
        on_end: typing.Optional[typing.Callable[[], None]] = None,
    ) -> None:
        self._busy_message = busy_message
        self._on_end = on_end
        self._guard = threading.Lock()
        self._current: typing.Optional[LeaseToken] = None

    def acquire(self) -> LeaseToken:
        with self._guard:
            if self._current is not None:
                raise RuntimeError(self._busy_message)
            self._current = LeaseToken()
            return self._current

    def release(self, token: LeaseToken) -> None:
        """End `token`; frees the lease only if `token` is the current holder."""
        with self._guard:
            if self._current is not token:
                token.ended.set()
                return
            self._end_current()

    def revoke(self) -> None:
        """End whichever holder there is, if any."""
        with self._guard:
            if self._current is not None:
                self._end_current()

    def _end_current(self) -> None:
        token = typing.cast(LeaseToken, self._current)
        self._current = None
        token.ended.set()
        if self._on_end is not None:
            self._on_end()


__all__ = [
    "CaptureOutput",
    "CommandArgument",
    "CommandLine",
    "CommandEnvironment",
    "ExecutionOptions",
    "Executor",
    "ExecutorCapability",
    "CommandLike",
    "StreamTarget",
    "CommandInput",
    "StrPath",
    "capture_streams",
    "dispatch_output",
    "expired",
    "normalize_environment",
    "normalize_input",
    "reject_stdin_conflict",
    "validate_environment_names",
    "wants_text",
    "write_output",
]
