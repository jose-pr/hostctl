"""Local subprocess executor."""

from __future__ import annotations

import os
import subprocess
import typing

from ._common import (
    CaptureOutput,
    command_text,
    CommandArgument,
    Environment,
    Executor,
    ExecutorCapability,
    ExecutorCommand,
    FileHandle,
    Input,
    normalize_environment,
    normalize_input,
    capture_streams,
    reject_stdin_conflict,
)


class LocalExecutor(Executor[subprocess.CompletedProcess]):
    """Execute direct argv or finalized shell invocations locally."""

    executor_capabilities = frozenset(
        (ExecutorCapability.ARGS, ExecutorCapability.CWD, ExecutorCapability.ENV)
    )

    def __call__(
        self,
        command: ExecutorCommand,
        *args: CommandArgument,
        bufsize: int = -1,
        stdin: typing.Optional[FileHandle] = None,
        stdout: typing.Optional[FileHandle] = None,
        stderr: typing.Optional[FileHandle] = None,
        cwd: typing.Optional[typing.Union[str, os.PathLike[str]]] = None,
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
            raise TypeError(f"unsupported local executor option: {sorted(options)[0]}")
        reject_stdin_conflict(input, stdin)
        stdout, stderr = capture_streams(capture_output, stdout, stderr)
        argv = [command_text(command), *(command_text(value) for value in args)]
        return subprocess.run(
            argv,
            bufsize=bufsize,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            cwd=os.fspath(cwd) if cwd is not None else None,
            env=normalize_environment(env),
            check=check,
            encoding=encoding,
            errors=errors,
            # `encoding`/`errors`/`text` put subprocess's stdin in text mode,
            # where bytes would kill the writer thread and hang the call.
            input=normalize_input(
                input,
                text_mode=bool(encoding or errors or text),
                encoding=encoding,
                errors=errors,
            ),
            timeout=timeout,
            text=text,
        )
