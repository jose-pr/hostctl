"""SSH command executor."""

from __future__ import annotations

import io
import inspect
import os
import subprocess
import typing

from ._common import (
    CaptureOutput,
    command_text,
    CommandArgument,
    Environment,
    Executor,
    ExecutorCommand,
    ExecutorCapability,
    FileHandle,
    Input,
    dispatch_output,
    normalize_environment,
    normalize_input,
    capture_streams,
    reject_stdin_conflict,
    wants_text,
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

    @staticmethod
    async def _abandon(process: object) -> bool:
        """Terminate and close a process whose wait() did not finish.

        Returns whether termination was actually attempted, which is what
        `TimeoutExpired.orphaned` reports.
        """
        attempted = False
        for name in ("terminate", "close"):
            method = getattr(process, name, None)
            if method is None:
                continue
            try:
                result = method()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                continue
            attempted = True
            break
        closer = getattr(process, "wait_closed", None)
        if closer is not None:
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
        return attempted

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
        if bufsize == 0:
            raise ValueError("bufsize=0 is unsupported by the buffered SSH executor")
        command = command_text(command)
        reject_stdin_conflict(input, stdin)
        if wants_text(text, encoding, errors) and encoding is None:
            # AsyncSSH returns `bytes` unless it is given an encoding, so
            # text mode has to be expressed as one.
            encoding = "utf-8"
        env = normalize_environment(env)

        from .. import _async

        stdout, stderr = capture_streams(capture_output, stdout, stderr)
        if input is not None:
            # The sink is a BytesIO, so this leg is always binary regardless of
            # the command's text encoding -- `text_mode=False` encodes str and
            # passes bytes through.
            value = normalize_input(
                input, text_mode=False, encoding=encoding, errors=errors
            )
            stdin = io.BytesIO(typing.cast(bytes, value))
        elif stdin is None:
            # AsyncSSH leaves a command's stdin open when no stream is passed.
            # Supplying an empty in-memory stream sends EOF and matches
            # subprocess.run()'s non-interactive default.
            stdin = _input_buffer(
                subprocess.DEVNULL,
                encoding=encoding,
                errors=errors,
            )
        else:
            stdin = _input_buffer(
                stdin,
                encoding=encoding,
                errors=errors,
            )

        stdout_target, stderr_target = stdout, stderr
        terminated = False
        # Resolved on the calling thread: the accessor connects, and
        # connecting runs on the bridge loop -- asking for it from inside the
        # loop waits on a future only that loop can complete.
        connection = self._connection()

        async def dispatch() -> object:
            # `connection.run()` is `create_process()` followed by
            # `process.wait()`, and it never hands the process back -- so a
            # timeout used to abandon a live channel and a still-running
            # remote command, with nothing to terminate and `orphaned` always
            # True. Creating the process here keeps the handle that
            # docs/guide/contracts.md requires for a best-effort terminate.
            nonlocal terminated
            process = await connection.create_process(
                command,
                bufsize=bufsize,
                stdin=stdin,
                stdout=None,
                stderr=(
                    subprocess.STDOUT if stderr_target == subprocess.STDOUT else None
                ),
                env=env,
                encoding=encoding,
                errors=errors,
            )
            try:
                return await process.wait(check=False, timeout=timeout)
            except BaseException:
                terminated = await SshExecutor._abandon(process)
                raise

        try:
            result = _async.async_to_sync(dispatch())
        except Exception as exc:
            normalized = _async.normalize_asyncssh_error(
                exc,
                command=command,
                timeout=timeout,
            )
            if isinstance(normalized, subprocess.TimeoutExpired):
                normalized.orphaned = not terminated
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

        returncode = result.returncode
        if returncode is None:
            returncode = -1
        completed = subprocess.CompletedProcess(
            args=command,
            returncode=returncode,
            stdout=result_stdout,
            stderr=result_stderr,
        )
        if check:
            completed.check_returncode()
        return completed
