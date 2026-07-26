"""Docker Engine command executor."""

from __future__ import annotations

import subprocess
import typing

from ._common import (
    CaptureOutput,
    CommandArgument,
    Environment,
    Executor,
    ExecutorCapability,
    ExecutorCommand,
    FileHandle,
    Input,
    PathLike,
    dispatch_output,
    normalize_environment,
)


class ContainerLike(typing.Protocol):
    """Small Docker SDK container surface used by :class:`ContainerExecutor`."""

    def exec_run(self, command: typing.Sequence[str], **options: object) -> object: ...


class ContainerExecutor(Executor[subprocess.CompletedProcess]):
    """Execute argv directly through a Docker Engine container exec."""

    executor_capabilities = frozenset(
        (ExecutorCapability.ARGS, ExecutorCapability.CWD, ExecutorCapability.ENV)
    )

    def __init__(
        self,
        container: typing.Callable[[], ContainerLike],
        *,
        user: typing.Optional[str] = None,
        workdir: typing.Optional[str] = None,
    ) -> None:
        self._container = container
        self._user = user
        self._workdir = workdir

    def __call__(
        self,
        command: ExecutorCommand,
        *args: CommandArgument,
        bufsize: int = -1,
        stdin: typing.Optional[FileHandle] = None,
        stdout: typing.Optional[FileHandle] = None,
        stderr: typing.Optional[FileHandle] = None,
        cwd: typing.Optional[PathLike] = None,
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
        del bufsize
        if options:
            raise TypeError(
                f"unsupported container executor option: {sorted(options)[0]}"
            )
        if stdin is not None or input is not None:
            raise NotImplementedError(
                "buffered container execution does not yet support stdin/input"
            )
        if timeout is not None:
            raise NotImplementedError(
                "Docker Engine exec does not provide a cancellable command timeout"
            )

        from ..host._common import capture_streams

        stdout, stderr = capture_streams(capture_output, stdout, stderr)
        argv = [str(command)]
        argv.extend(
            value.decode() if isinstance(value, bytes) else str(value) for value in args
        )
        exec_options: typing.Dict[str, object] = {
            "stdout": True,
            "stderr": True,
            "demux": True,
        }
        normalized_env = normalize_environment(env)
        if normalized_env is not None:
            exec_options["environment"] = normalized_env
        selected_workdir = str(cwd) if cwd is not None else self._workdir
        if selected_workdir is not None:
            exec_options["workdir"] = selected_workdir
        if self._user is not None:
            exec_options["user"] = self._user

        try:
            result = self._container().exec_run(argv, **exec_options)
        except Exception as exc:
            normalized = normalize_container_error(exc)
            if normalized is exc:
                raise
            raise normalized from exc

        returncode = typing.cast(int, getattr(result, "exit_code"))
        output = getattr(result, "output")
        if isinstance(output, tuple):
            out, err = output
        else:
            out, err = output, None
        if text or encoding is not None or errors is not None:
            codec = encoding or "utf-8"
            out = (
                out.decode(codec, errors or "strict") if isinstance(out, bytes) else out
            )
            err = (
                err.decode(codec, errors or "strict") if isinstance(err, bytes) else err
            )
        if stderr is subprocess.STDOUT:
            if err:
                empty = "" if isinstance(err, str) else b""
                out = (out or empty) + err
            err = None
        out, err = dispatch_output(
            stdout, stderr, out, err, encoding=encoding, errors=errors
        )
        completed = subprocess.CompletedProcess(argv, returncode, out, err)
        if check:
            completed.check_returncode()
        return completed


def normalize_container_error(exc: Exception) -> Exception:
    """Map Docker SDK and HTTP failures without importing the optional SDK."""
    name = type(exc).__name__
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    if status_code == 404 or name in ("NotFound",):
        return FileNotFoundError(str(exc))
    if status_code in (401, 403) or name in ("AuthenticationError",):
        return PermissionError(str(exc))
    if name in ("ReadTimeout", "ConnectTimeout", "Timeout"):
        return TimeoutError(str(exc))
    if name in (
        "APIError",
        "ConnectionError",
        "DockerException",
        "MaxRetryError",
    ):
        return ConnectionError(str(exc))
    return exc
