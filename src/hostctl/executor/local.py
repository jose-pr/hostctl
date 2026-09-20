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
    wants_text,
)


def _merged_environment(env):
    """`env` merged over the inherited environment, or None when unset.

    A truly empty environment is deliberately not expressible here: it is a
    separate contract (see the shipped header), and it is dangerous -- a
    replacing environment that omits PATH/SystemRoot stops powershell.exe
    from starting at all on Windows.
    """
    values = normalize_environment(env)
    if values is None:
        return None
    merged = dict(os.environ)
    merged.update(values)
    return merged


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
            # Additive, like every other transport. `subprocess.run(env=...)`
            # replaces the environment, so the one documented cross-provider
            # rule -- "env is additive to the provider's environment" -- held
            # for SSH, WinRM, container and QGA and was inverted for the
            # local executor. A SystemHost that fell back to local therefore
            # handed the child no PATH where the same call over SSH kept it,
            # which on Windows is enough to stop powershell.exe starting.
            env=_merged_environment(env),
            check=check,
            encoding=encoding,
            errors=errors,
            # `encoding`/`errors`/`text` put subprocess's stdin in text mode,
            # where bytes would kill the writer thread and hang the call.
            input=normalize_input(
                input,
                text_mode=wants_text(text, encoding, errors),
                encoding=encoding,
                errors=errors,
            ),
            timeout=timeout,
            text=text,
        )
