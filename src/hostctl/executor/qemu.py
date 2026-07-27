"""Buffered command execution through the QEMU Guest Agent."""

from __future__ import annotations

import base64
import os
import subprocess
import time
import typing

from ._qga import GuestAgentTransport, QgaProtocolError
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
    normalize_input,
    capture_streams,
    reject_stdin_conflict,
)


class GuestAgentProtocolError(QgaProtocolError):
    """A guest-agent response did not satisfy the QGA command contract."""


def _read_input(
    stream: FileHandle,
    *,
    encoding: typing.Optional[str],
    errors: typing.Optional[str],
) -> bytes:
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


def _decode_output(value: object, field: str) -> bytes:
    if value is None:
        return b""
    if not isinstance(value, str):
        raise GuestAgentProtocolError(f"{field} must be Base64 text")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise GuestAgentProtocolError(f"{field} is not valid Base64") from exc


class QemuExecutor(Executor[subprocess.CompletedProcess]):
    """Execute direct argv through QGA's buffered ``guest-exec`` RPC."""

    executor_capabilities = frozenset((ExecutorCapability.ARGS, ExecutorCapability.ENV))

    def __init__(
        self,
        transport: typing.Callable[[], GuestAgentTransport],
        *,
        poll_interval: float = 0.01,
        max_poll_interval: float = 0.25,
        clock: typing.Callable[[], float] = time.monotonic,
        sleep: typing.Callable[[float], None] = time.sleep,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if max_poll_interval < poll_interval:
            raise ValueError("max_poll_interval must be at least poll_interval")
        self._transport = transport
        self._poll_interval = poll_interval
        self._max_poll_interval = max_poll_interval
        self._clock = clock
        self._sleep = sleep

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
            raise TypeError(f"unsupported QEMU executor option: {sorted(options)[0]}")
        if cwd is not None:
            raise NotImplementedError(
                "QGA guest-exec has no native working-directory support"
            )
        reject_stdin_conflict(input, stdin)
        if not str(command):
            raise ValueError("QGA guest-exec path must not be empty")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must not be negative")
        if text and encoding is None:
            encoding = "utf-8"

        stdout, stderr = capture_streams(capture_output, stdout, stderr)
        argv = [
            (
                value.decode(encoding or "utf-8", errors or "strict")
                if isinstance(value, bytes)
                else str(value)
            )
            for value in args
        ]
        arguments: typing.Dict[str, object] = {
            "path": str(command),
            "arg": argv,
            "capture-output": True,
        }
        normalized_env = normalize_environment(env)
        if normalized_env is not None:
            arguments["env"] = [
                f"{key}={value}" for key, value in normalized_env.items()
            ]
        if input is not None:
            # QGA carries stdin base64-encoded, so this leg is always binary.
            payload = normalize_input(
                input, text_mode=False, encoding=encoding, errors=errors
            )
            arguments["input-data"] = base64.b64encode(
                typing.cast(bytes, payload)
            ).decode("ascii")
        elif stdin is not None:
            payload = _read_input(
                stdin,
                encoding=encoding,
                errors=errors,
            )
            arguments["input-data"] = base64.b64encode(payload).decode("ascii")

        deadline = None if timeout is None else self._clock() + timeout
        command_display = [str(command), *argv]
        started = self._execute(
            {"execute": "guest-exec", "arguments": arguments},
            deadline,
            command_display,
            timeout=timeout,
        )
        if not isinstance(started, typing.Mapping):
            raise GuestAgentProtocolError("guest-exec returned a non-object result")
        pid = started.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool):
            raise GuestAgentProtocolError("guest-exec did not return an integer PID")

        status = self._wait(pid, deadline, command_display, timeout)
        out = _decode_output(status.get("out-data"), "out-data")
        err = _decode_output(status.get("err-data"), "err-data")
        returncode = status.get("exitcode")
        if not isinstance(returncode, int) or isinstance(returncode, bool):
            signal = status.get("signal")
            if isinstance(signal, int) and not isinstance(signal, bool):
                returncode = -signal
            else:
                raise GuestAgentProtocolError(
                    "completed guest-exec status has no integer exit code or signal"
                )

        if text or encoding is not None or errors is not None:
            codec = encoding or "utf-8"
            out = out.decode(codec, errors or "strict")
            err = err.decode(codec, errors or "strict")
        if stderr is subprocess.STDOUT:
            out += err
            err = None
        out, err = dispatch_output(
            stdout,
            stderr,
            out,
            err,
            encoding=encoding,
            errors=errors,
        )
        completed = subprocess.CompletedProcess(
            command_display,
            returncode,
            out,
            err,
        )
        completed.pid = pid
        completed.stdout_truncated = bool(status.get("out-truncated", False))
        completed.stderr_truncated = bool(status.get("err-truncated", False))
        if check:
            completed.check_returncode()
        return completed

    def _execute(
        self,
        request: typing.Mapping[str, object],
        deadline: typing.Optional[float],
        command: typing.Sequence[str],
        timeout: typing.Optional[float] = None,
    ) -> object:
        remaining = self._remaining(deadline, command, timeout=timeout)
        try:
            return self._transport().execute(request, timeout=remaining)
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            expired = subprocess.TimeoutExpired(command, remaining)
            expired.orphaned = request.get("execute") == "guest-exec"
            raise expired from exc

    def _wait(
        self,
        pid: int,
        deadline: typing.Optional[float],
        command: typing.Sequence[str],
        timeout: typing.Optional[float],
    ) -> typing.Mapping[str, object]:
        interval = self._poll_interval
        partial_out = b""
        partial_err = b""
        while True:
            try:
                result = self._execute(
                    {
                        "execute": "guest-exec-status",
                        "arguments": {"pid": pid},
                    },
                    deadline,
                    command,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                self._annotate_timeout(exc, pid, timeout, partial_out, partial_err)
                raise
            if not isinstance(result, typing.Mapping):
                raise GuestAgentProtocolError(
                    "guest-exec-status returned a non-object result"
                )
            if not isinstance(result.get("exited"), bool):
                raise GuestAgentProtocolError(
                    "guest-exec-status has no boolean exited field"
                )
            if "out-data" in result:
                partial_out = _decode_output(result.get("out-data"), "out-data")
            if "err-data" in result:
                partial_err = _decode_output(result.get("err-data"), "err-data")
            if result.get("exited") is True:
                return result
            try:
                remaining = self._remaining(deadline, command, pid, timeout)
            except subprocess.TimeoutExpired as exc:
                self._annotate_timeout(exc, pid, timeout, partial_out, partial_err)
                raise
            delay = interval if remaining is None else min(interval, remaining)
            self._sleep(delay)
            interval = min(interval * 2, self._max_poll_interval)

    @staticmethod
    def _annotate_timeout(
        exc: subprocess.TimeoutExpired,
        pid: int,
        timeout: typing.Optional[float],
        stdout: bytes,
        stderr: bytes,
    ) -> None:
        exc.timeout = timeout
        exc.pid = pid
        exc.orphaned = True
        exc.output = stdout
        exc.stderr = stderr

    def _remaining(
        self,
        deadline: typing.Optional[float],
        command: typing.Sequence[str],
        pid: typing.Optional[int] = None,
        timeout: typing.Optional[float] = None,
    ) -> typing.Optional[float]:
        if deadline is None:
            return None
        remaining = deadline - self._clock()
        if remaining > 0:
            return remaining
        expired = subprocess.TimeoutExpired(command, timeout)
        expired.orphaned = pid is not None
        if pid is not None:
            expired.pid = pid
        raise expired
