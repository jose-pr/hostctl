"""SSH command executor."""

from __future__ import annotations

import io
import os
import subprocess
import typing

from ._common import (
    CaptureOutput,
    CommandArgument,
    Environment,
    Executor,
    ExecutorCommand,
    ExecutorCapability,
    FileHandle,
    Input,
    dispatch_output,
    normalize_environment,
    capture_streams,
    reject_stdin_conflict,
)


class _SshResult(typing.Protocol):
    returncode: int
    stdout: typing.Optional[typing.Union[str, bytes]]
    stderr: typing.Optional[typing.Union[str, bytes]]


class SshConnection(typing.Protocol):
    def is_closed(self) -> bool: ...

    def close(self) -> None: ...

    def wait_closed(self) -> typing.Awaitable[None]: ...

    def run(self, command: str, **options: object) -> typing.Awaitable[_SshResult]: ...

    def create_process(
        self, command: typing.Optional[str] = None, **options: object
    ) -> typing.Awaitable[object]: ...


def _input_buffer(
    stream: FileHandle,
    *,
    encoding: typing.Optional[str],
    errors: typing.Optional[str],
) -> io.BytesIO:
    if stream == subprocess.DEVNULL:
        return io.BytesIO()
    if stream == subprocess.PIPE:
        raise ValueError("stdin=subprocess.PIPE requires input")
    if isinstance(stream, int):
        with os.fdopen(os.dup(stream), "rb") as duplicate:
            value = duplicate.read()
    else:
        value = stream.read()
    if isinstance(value, str):
        value = value.encode(encoding or "utf-8", errors or "strict")
    return io.BytesIO(value)


class SshExecutor(Executor[subprocess.CompletedProcess]):
    """Execute finalized command strings through an AsyncSSH connection."""

    executor_capabilities: typing.FrozenSet[ExecutorCapability] = frozenset()

    def __init__(self, connection: typing.Callable[[], SshConnection]) -> None:
        self._connection = connection

    def __call__(
        self,
        command: ExecutorCommand,
        *args: CommandArgument,
        bufsize: int = -1,
        stdin: typing.Optional[FileHandle] = None,
        stdout: typing.Optional[FileHandle] = None,
        stderr: typing.Optional[FileHandle] = None,
        env: typing.Optional[Environment] = None,
        capture_output: CaptureOutput = True,
        check: bool = True,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
        input: Input = None,
        timeout: typing.Optional[float] = None,
        text: typing.Optional[bool] = None,
        **options: object,
    ) -> subprocess.CompletedProcess:
        if options:
            raise TypeError(f"unsupported SSH executor option: {sorted(options)[0]}")
        if args:
            raise NotImplementedError("SshExecutor does not support native arguments")
        command = str(command)
        reject_stdin_conflict(input, stdin)
        if text and encoding is None:
            encoding = "utf-8"
        env = normalize_environment(env)

        from .. import _async

        stdout, stderr = capture_streams(capture_output, stdout, stderr)
        if input is not None:
            value = (
                input
                if isinstance(input, bytes)
                else input.encode(encoding or "utf-8", errors or "strict")
            )
            stdin = io.BytesIO(value)
        elif stdin is not None:
            stdin = _input_buffer(
                stdin,
                encoding=encoding,
                errors=errors,
            )

        stdout_target, stderr_target = stdout, stderr
        try:
            result = _async.async_to_sync(
                self._connection().run(
                    command,
                    bufsize=bufsize,
                    stdin=stdin,
                    stdout=None,
                    stderr=(
                        subprocess.STDOUT
                        if stderr_target == subprocess.STDOUT
                        else None
                    ),
                    env=env,
                    check=False,
                    encoding=encoding,
                    errors=errors,
                    timeout=timeout,
                )
            )
        except Exception as exc:
            normalized = _async.normalize_asyncssh_error(
                exc,
                command=command,
                timeout=timeout,
            )
            if normalized is exc:
                raise
            raise normalized from exc

        result_stdout = result.stdout
        result_stderr = None if stderr_target == subprocess.STDOUT else result.stderr
        result_stdout, result_stderr = dispatch_output(
            stdout_target,
            stderr_target,
            result_stdout,
            result_stderr,
            encoding=encoding,
            errors=errors,
        )

        completed = subprocess.CompletedProcess(
            args=command,
            returncode=result.returncode,
            stdout=result_stdout,
            stderr=result_stderr,
        )
        if check:
            completed.check_returncode()
        return completed
