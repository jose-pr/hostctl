"""Transport-independent command executor contracts."""

from __future__ import annotations

import enum
import os
import subprocess
import sys
import typing
from pathlib import PurePath

from pathlib_next import Pathname

_Result = typing.TypeVar("_Result", covariant=True)

FileHandle = typing.Union[int, typing.BinaryIO, typing.TextIO]
Input = typing.Optional[typing.Union[bytes, str]]
PathLike = typing.Union[str, os.PathLike[str]]
Environment = typing.Mapping[typing.Union[str, bytes], object]
CaptureOutput = typing.Literal[True, False, "stdout", "stderr"]
ExecutorCommand = typing.Union[str, PurePath, Pathname]
CommandArgument = typing.Union[str, bytes, PurePath, Pathname]


def capture_streams(
    capture_output: CaptureOutput,
    stdout: typing.Optional[FileHandle],
    stderr: typing.Optional[FileHandle],
) -> typing.Tuple[typing.Optional[FileHandle], typing.Optional[FileHandle]]:
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


def reject_stdin_conflict(input: Input, stdin: typing.Optional[FileHandle]) -> None:
    if input is not None and stdin is not None:
        raise ValueError("stdin and input arguments may not both be used")


def normalize_input(
    input: Input,
    *,
    text_mode: bool,
    encoding: typing.Optional[str] = None,
    errors: typing.Optional[str] = None,
) -> Input:
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
    if input is None or isinstance(input, (int,)):
        return input
    if text_mode:
        if isinstance(input, bytes):
            return input.decode(encoding or "utf-8", errors or "strict")
        return input
    if isinstance(input, str):
        return input.encode(encoding or "utf-8", errors or "strict")
    return input


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


def normalize_environment(
    env: typing.Optional[Environment],
) -> typing.Optional[typing.Dict[str, str]]:
    if env is None:
        return None
    return {
        key.decode() if isinstance(key, bytes) else str(key): (
            value.decode() if isinstance(value, bytes) else str(value)
        )
        for key, value in env.items()
    }


def write_output(
    stream: FileHandle,
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
            if isinstance(value, bytes):
                stream.write(value.decode(encoding or "utf-8", errors or "strict"))
            else:
                stream.write(value.encode(encoding or "utf-8", errors or "strict"))
        flush = getattr(stream, "flush", None)
        if flush is not None:
            flush()
    finally:
        if close:
            stream.close()


def dispatch_output(
    stdout_target: typing.Optional[FileHandle],
    stderr_target: typing.Optional[FileHandle],
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

    stdin: typing.Optional[FileHandle]
    stdout: typing.Optional[FileHandle]
    stderr: typing.Optional[FileHandle]
    cwd: typing.Optional[PathLike]
    env: typing.Optional[Environment]
    capture_output: CaptureOutput
    check: bool
    encoding: typing.Optional[str]
    errors: typing.Optional[str]
    input: Input
    timeout: typing.Optional[float]
    text: bool


@typing.runtime_checkable
class Executor(typing.Protocol[_Result]):
    """Callable executor receiving one command string and execution options."""

    executor_capabilities: typing.FrozenSet[ExecutorCapability]

    def __call__(
        self,
        command: ExecutorCommand,
        *args: CommandArgument,
        stdin: typing.Optional[FileHandle] = None,
        stdout: typing.Optional[FileHandle] = None,
        stderr: typing.Optional[FileHandle] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
        capture_output: typing.Optional[CaptureOutput] = None,
        check: typing.Optional[bool] = None,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
        input: Input = None,
        timeout: typing.Optional[float] = None,
        text: typing.Optional[bool] = None,
        **options: object,
    ) -> _Result: ...
