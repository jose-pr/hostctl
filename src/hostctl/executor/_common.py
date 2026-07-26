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


class ExecutorCapability(enum.Enum):
    ARGS = "args"
    CWD = "cwd"
    ENV = "env"


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
