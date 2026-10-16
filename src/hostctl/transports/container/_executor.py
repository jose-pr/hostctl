"""Docker Engine command executor."""

from __future__ import annotations

import typing

from ...executor import BufferedExecutor, ExecutorCapability, ExecutorRequest


class ContainerLike(typing.Protocol):
    """Small Docker SDK container surface used by :class:`ContainerExecutor`."""

    def exec_run(self, command: typing.Sequence[str], **options: object) -> object: ...


class ContainerExecutor(BufferedExecutor):
    """Execute argv directly through a Docker Engine container exec."""

    executor_capabilities = frozenset(
        (ExecutorCapability.ARGS, ExecutorCapability.CWD, ExecutorCapability.ENV)
    )
    input_unsupported = "buffered container execution does not yet support stdin/input"
    timeout_unsupported = (
        "Docker Engine exec does not provide a cancellable command timeout"
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

    def _exchange(self, request: ExecutorRequest):
        exec_options: typing.Dict[str, object] = {
            "stdout": True,
            "stderr": True,
            # Docker's demux=False stream preserves stdout/stderr ordering,
            # which is required when callers request STDERR->STDOUT.
            "demux": not request.merge_stderr,
        }
        if request.env is not None:
            exec_options["environment"] = request.env
        workdir = request.cwd if request.cwd is not None else self._workdir
        if workdir is not None:
            exec_options["workdir"] = workdir
        if self._user is not None:
            exec_options["user"] = self._user
        result = self._container().exec_run(
            [request.command, *request.args], **exec_options
        )
        # docker-py documents a null ExitCode; the base reports it as -1 so
        # that `check=True` raises.
        output = getattr(result, "output")
        out, err = output if isinstance(output, tuple) else (output, None)
        return out, err, getattr(result, "exit_code")

    def _normalize_error(self, exc: Exception) -> Exception:
        if _is_not_found(exc):
            return ConnectionError("container not found")
        return normalize_container_error(exc)


def normalize_container_error(exc: Exception) -> Exception:
    """Map Docker SDK and HTTP failures without importing the optional SDK."""
    name = type(exc).__name__
    status_code = _status_code(exc)
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


def _status_code(exc: BaseException) -> typing.Optional[int]:
    """The HTTP status a Docker SDK or HTTP error carries, wherever it keeps it."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status


def _is_not_found(exc: BaseException) -> bool:
    return _status_code(exc) == 404 or type(exc).__name__ == "NotFound"
