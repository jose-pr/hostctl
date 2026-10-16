"""Transport-independent command executor contracts."""

from __future__ import annotations

import codecs
import contextvars
import dataclasses
import enum
import os
import re
import subprocess
import sys
import threading
import time
import typing
from pathlib import PurePath

from pathapi import Pathname

from .exceptions import OperationNotStartedError

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


def _read_stdin(
    stream: StreamTarget,
    *,
    encoding: typing.Optional[str],
    errors: typing.Optional[str],
) -> bytes:
    """Read a `stdin=` target to EOF, for a transport that sends input whole.

    `DEVNULL` is empty input; `PIPE` has nothing to read without `input=`. A
    descriptor is duplicated so the caller's stays open.
    """
    if stream == subprocess.DEVNULL:
        return b""
    if stream == subprocess.PIPE:
        raise ValueError("stdin=subprocess.PIPE requires input")
    if isinstance(stream, int):
        with os.fdopen(os.dup(stream), "rb") as duplicate:
            value = duplicate.read()
    else:
        value = stream.read()
    if isinstance(value, str):
        return value.encode(encoding or "utf-8", errors or "strict")
    return value


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


class _Omitted(enum.Enum):
    """`timeout=` left out of a call: the configured default applies.

    Distinct from `None`, which is "no limit for this call".
    """

    OMITTED = "omitted"

    def __repr__(self) -> str:
        return "<config timeout>"


_OMITTED = _Omitted.OMITTED


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


#: Captured output in either type, or `None` for none.
_Output = typing.Optional[typing.Union[bytes, str]]


@dataclasses.dataclass(frozen=True)
class ExecutorRequest:
    """One command as a transport primitive of `BufferedExecutor` receives it.

    `command` is the command text and `args` the native arguments as text,
    empty unless the executor declares `args`. `input` is the whole standard
    input, or `None` when the call sent none: the command then gets an empty
    stdin (EOF), never a terminal. `merge_stderr` is `stderr=STDOUT`: merge
    natively where the transport can, else return stderr apart and the base
    appends it. `cwd` and `env` are `None` unless declared and passed; `env`
    adds to the target's environment. `timeout` is the call's limit in seconds
    (`None`: no limit) and `deadline` the same limit on `time.monotonic()`.
    """

    command: str
    args: typing.Tuple[str, ...] = ()
    input: typing.Optional[bytes] = None
    merge_stderr: bool = False
    cwd: typing.Optional[str] = None
    env: typing.Optional[typing.Dict[str, str]] = None
    timeout: typing.Optional[float] = None
    deadline: typing.Optional[float] = None

    def remaining(self) -> typing.Optional[float]:
        """Seconds left before `deadline`, zero or less once it passed; `None` without one."""
        if self.deadline is None:
            return None
        return self.deadline - time.monotonic()


def _in_mode(
    value: typing.Optional[typing.Union[bytes, bytearray, str]],
    text: bool,
    encoding: typing.Optional[str],
    errors: typing.Optional[str],
    *,
    lenient: bool = False,
) -> _Output:
    """`value` as the call's output type: `str` in text mode, else `bytes`."""
    if value is None:
        return None
    if text:
        if isinstance(value, str):
            return value
        # A timeout cuts output at an arbitrary byte; a strict decode of the
        # torn character would replace the `TimeoutExpired` with an error.
        fallback = "replace" if lenient else "strict"
        return bytes(value).decode(encoding or "utf-8", errors or fallback)
    if isinstance(value, str):
        return value.encode(encoding or "utf-8", errors or "strict")
    return bytes(value)


class _Call(typing.NamedTuple):
    """What one `__call__` resolved, shared by both dispatch shapes."""

    display: typing.Union[str, typing.List[str]]
    stdout: typing.Optional[StreamTarget]
    stderr: typing.Optional[StreamTarget]
    text: bool
    encoding: typing.Optional[str]
    errors: typing.Optional[str]

    @property
    def mode(self) -> typing.Tuple[bool, typing.Optional[str], typing.Optional[str]]:
        """`_in_mode`'s arguments for this call."""
        return self.text, self.encoding, self.errors

    def expired(
        self,
        error: subprocess.TimeoutExpired,
        request: ExecutorRequest,
        *,
        output: _Output = None,
        stderr: _Output = None,
        orphaned: typing.Optional[bool] = None,
    ) -> subprocess.TimeoutExpired:
        """`error` in hostctl's shape: this call's command and output type."""
        return expired(
            self.display,
            error.timeout if error.timeout is not None else request.timeout,
            output=_in_mode(
                error.output if output is None else output,
                self.text,
                self.encoding,
                self.errors,
                lenient=True,
            ),
            stderr=_in_mode(
                error.stderr if stderr is None else stderr,
                self.text,
                self.encoding,
                self.errors,
                lenient=True,
            ),
            orphaned=(
                getattr(error, "orphaned", True) if orphaned is None else orphaned
            ),
            pid=getattr(error, "pid", None),
            text=self.text,
        )


class BufferedExecutor(Executor[subprocess.CompletedProcess]):
    """Base of an executor whose transport returns a command's whole output.

    A subclass declares `executor_capabilities` and implements `_exchange`.
    The base implements the subprocess-shaped `__call__` every hostctl
    executor shares: unknown options and undeclared `args`/`cwd`/`env` are
    refused, `input=`/`stdin=` become bytes, the output type follows
    `wants_text`, capture and stream targets follow `capture_streams` and
    `dispatch_output`, a non-zero status raises under `check=True` (the
    default), and a timeout raises `expired()`'s `TimeoutExpired`.
    """

    executor_capabilities: typing.FrozenSet[str] = frozenset()
    #: Why the transport cannot take `stdin=`/`input=`, or `None` when it can.
    #: When set, passing either raises `NotImplementedError` with this text
    #: before the caller's stream is read.
    input_unsupported: typing.Optional[str] = None
    #: Why the transport cannot bound a call, or `None` when it can. When set,
    #: a `timeout=` other than `None` raises `NotImplementedError` with it.
    timeout_unsupported: typing.Optional[str] = None

    def __call__(
        self,
        command: CommandLike,
        *args: CommandArgument,
        bufsize: int = -1,
        stdin: typing.Optional[StreamTarget] = None,
        stdout: typing.Optional[StreamTarget] = None,
        stderr: typing.Optional[StreamTarget] = None,
        cwd: typing.Optional[StrPath] = None,
        env: typing.Optional[CommandEnvironment] = None,
        capture_output: CaptureOutput = True,
        check: bool = True,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
        input: CommandInput = None,
        timeout: typing.Optional[float] = _OMITTED,  # type: ignore[assignment]
        text: typing.Optional[bool] = None,
        **options: object,
    ) -> subprocess.CompletedProcess:
        del bufsize
        name = type(self).__name__
        if options:
            raise TypeError(f"unsupported {name} option: {sorted(options)[0]}")
        capabilities = self.executor_capabilities
        if args and ExecutorCapability.ARGS not in capabilities:
            raise NotImplementedError(f"{name} takes no native arguments")
        if cwd is not None and ExecutorCapability.CWD not in capabilities:
            raise NotImplementedError(f"{name} has no native working directory")
        if env is not None and ExecutorCapability.ENV not in capabilities:
            raise NotImplementedError(f"{name} has no native environment")
        if self.input_unsupported is not None and (
            stdin is not None or input is not None
        ):
            raise NotImplementedError(self.input_unsupported)
        if timeout is _OMITTED:
            timeout = self._default_timeout()
        if timeout is not None and self.timeout_unsupported is not None:
            raise NotImplementedError(self.timeout_unsupported)
        reject_stdin_conflict(input, stdin)
        stdout, stderr = capture_streams(capture_output, stdout, stderr)
        environment = normalize_environment(env)
        payload: typing.Optional[bytes]
        if input is not None:
            # Whole bytes whatever the call's mode: the transport sends them.
            payload = typing.cast(
                bytes,
                normalize_input(
                    input, text_mode=False, encoding=encoding, errors=errors
                ),
            )
        elif stdin is not None:
            payload = _read_stdin(stdin, encoding=encoding, errors=errors)
        else:
            payload = None
        request = ExecutorRequest(
            command=command_text(command),
            args=tuple(command_text(value) for value in args),
            input=payload,
            merge_stderr=stderr == subprocess.STDOUT,
            cwd=None if cwd is None else command_text(cwd),
            env=environment,
            timeout=timeout,
            deadline=None if timeout is None else time.monotonic() + timeout,
        )
        call = _Call(
            (
                [request.command, *request.args]
                if ExecutorCapability.ARGS in capabilities
                else request.command
            ),
            stdout,
            stderr,
            wants_text(text, encoding, errors),
            encoding,
            errors,
        )
        out, err, returncode = self._dispatch_request(request, call)
        # `CompletedProcess` reads a `None` status as success.
        completed = subprocess.CompletedProcess(
            call.display, -1 if returncode is None else returncode, out, err
        )
        if check:
            completed.check_returncode()
        return completed

    def _exchange(
        self, request: ExecutorRequest
    ) -> typing.Tuple[_Output, _Output, typing.Optional[int]]:
        """Run `request` and return `(stdout, stderr, returncode)`; override it.

        Either stream may be `bytes`, `str` or `None` (empty). Raise
        `subprocess.TimeoutExpired` when `request.timeout` passes (build it
        with `expired()` to set `orphaned`, `pid` and partial output), and
        `OperationNotStartedError` only when nothing was sent.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement _exchange")

    def _default_timeout(self) -> typing.Optional[float]:
        """The limit of a call made without `timeout=`; `None` (no limit) here."""
        return None

    def _normalize_error(self, exc: Exception) -> Exception:
        """Map a transport error to the one to raise; `exc` itself by default.

        A `subprocess.TimeoutExpired` returned here is raised in `expired()`'s
        shape; anything else is raised chained to `exc`.
        """
        return exc

    @staticmethod
    def _raise_normalized(
        exc: Exception, normalized: Exception, request: ExecutorRequest, call: _Call
    ) -> "typing.NoReturn":
        if isinstance(normalized, subprocess.TimeoutExpired):
            raise call.expired(normalized, request) from exc
        if normalized is exc:
            raise exc
        raise normalized from exc

    def _dispatch_request(
        self, request: ExecutorRequest, call: _Call
    ) -> typing.Tuple[_Output, _Output, typing.Optional[int]]:
        try:
            out, err, returncode = self._exchange(request)
        except OperationNotStartedError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise call.expired(exc, request) from exc
        except Exception as exc:
            self._raise_normalized(exc, self._normalize_error(exc), request, call)
        out = _in_mode(b"" if out is None else out, *call.mode)
        err = _in_mode(b"" if err is None else err, *call.mode)
        if request.merge_stderr:
            if err:
                out = typing.cast(typing.Any, out) + err
            err = None
        out, err = dispatch_output(
            call.stdout,
            call.stderr,
            out,
            err,
            encoding=call.encoding,
            errors=call.errors,
        )
        return out, err, returncode


class _Sink:
    """One streamed output target, decoding incrementally in text mode."""

    def __init__(self, target: typing.Optional[StreamTarget], default, call: _Call):
        self._target = default if target is None else target
        self._call = call
        self._decoder = (
            codecs.getincrementaldecoder(call.encoding or "utf-8")(
                call.errors or "strict"
            )
            if call.text and self._target != subprocess.DEVNULL
            else None
        )

    def write(self, data: bytes, *, final: bool = False) -> None:
        if self._target == subprocess.DEVNULL:
            return
        value: typing.Union[bytes, str] = data
        if self._decoder is not None:
            value = self._decoder.decode(data, final)
        if value:
            write_output(
                self._target,
                value,
                encoding=self._call.encoding,
                errors=self._call.errors,
            )


class StreamingExecutor(BufferedExecutor):
    """Base of an executor whose transport delivers output as it arrives.

    A subclass implements `_send` and `_receive`, and `_abandon` when it can
    stop a running command. The base sends, then receives until a status
    arrives: captured output is collected, and output routed elsewhere (a
    file, `None` for the console) is written as each piece arrives. When the
    deadline passes it calls `_abandon` and raises `TimeoutExpired` carrying
    the output captured so far. `OperationNotStartedError` declines the
    provider only from `_send`; out of `_receive`, after the command was
    sent, it is raised as `ConnectionError` so the command is not replayed.
    """

    def _send(self, request: ExecutorRequest) -> None:
        """Send `request` (command and input) to the target; override it.

        Raise `OperationNotStartedError` only when no byte of it was sent.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement _send")

    def _receive(
        self, request: ExecutorRequest, wait: typing.Optional[float]
    ) -> typing.Tuple[bytes, bytes, typing.Optional[int]]:
        """Return the next `(stdout, stderr, returncode)` output; override it.

        Wait at most `wait` seconds (`None`: until something arrives) and
        return `(b"", b"", None)` when nothing did. A status other than
        `None` ends the command; output returned with it is its last.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement _receive")

    def _abandon(self, request: ExecutorRequest) -> bool:
        """Stop the command after a timeout or a failure; True if it may still run.

        The default stops nothing, so the command is reported orphaned.
        """
        return True

    def _stop(self, request: ExecutorRequest) -> bool:
        try:
            return bool(self._abandon(request))
        except Exception:
            return True

    def _dispatch_request(
        self, request: ExecutorRequest, call: _Call
    ) -> typing.Tuple[_Output, _Output, typing.Optional[int]]:
        captured_out = bytearray() if call.stdout == subprocess.PIPE else None
        captured_err = bytearray() if call.stderr == subprocess.PIPE else None
        out_sink = (
            None if captured_out is not None else _Sink(call.stdout, sys.stdout, call)
        )
        err_sink = (
            None
            if captured_err is not None or request.merge_stderr
            else _Sink(call.stderr, sys.stderr, call)
        )

        def collect(data: bytes, captured, sink) -> None:
            if not data:
                return
            if captured is not None:
                captured += data
            else:
                sink.write(data)

        sent = False
        deadline_passed: typing.Optional[subprocess.TimeoutExpired] = None
        try:
            self._send(request)
            sent = True
            while True:
                wait = request.remaining()
                if wait is not None and wait <= 0:
                    deadline_passed = expired(call.display, request.timeout)
                    raise deadline_passed
                out, err, returncode = self._receive(request, wait)
                if request.merge_stderr:
                    out, err = (out or b"") + (err or b""), b""
                collect(out, captured_out, out_sink)
                collect(err, captured_err, err_sink)
                if returncode is not None:
                    break
        except subprocess.TimeoutExpired as exc:
            orphaned = self._stop(request)
            raise call.expired(
                exc,
                request,
                output=b"" if captured_out is None else bytes(captured_out),
                stderr=b"" if captured_err is None else bytes(captured_err),
                orphaned=orphaned,
            ) from (None if exc is deadline_passed else exc)
        except Exception as exc:
            normalized = (
                exc
                if isinstance(exc, OperationNotStartedError)
                else self._normalize_error(exc)
            )
            declined = isinstance(normalized, OperationNotStartedError)
            if sent or not declined:
                self._stop(request)
            if sent and declined:
                # The command is out: replaying it elsewhere could run it twice.
                raise ConnectionError(
                    f"{typing.cast(OperationNotStartedError, normalized).reason}; "
                    "the command was already sent, so it is not retried through "
                    "another provider"
                ) from exc
            self._raise_normalized(exc, normalized, request, call)
        except BaseException:
            self._stop(request)
            raise
        for sink in (out_sink, err_sink):
            if sink is not None:
                sink.write(b"", final=True)
        return (
            None if captured_out is None else _in_mode(captured_out, *call.mode),
            None if captured_err is None else _in_mode(captured_err, *call.mode),
            returncode,
        )


__all__ = [
    "BufferedExecutor",
    "CaptureOutput",
    "CommandArgument",
    "CommandLine",
    "CommandEnvironment",
    "ExecutionOptions",
    "Executor",
    "ExecutorCapability",
    "ExecutorRequest",
    "StreamingExecutor",
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
