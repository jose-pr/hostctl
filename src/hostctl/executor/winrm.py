"""WinRM PowerShell script executor."""

from __future__ import annotations

import base64
import dataclasses
import subprocess
import typing

from ._common import (
    CaptureOutput,
    CommandArgument,
    Executor,
    ExecutorCommand,
    ExecutorCapability,
    FileHandle,
    Input,
    dispatch_output,
    capture_streams,
)


class _WinRMResponse(typing.Protocol):
    status_code: int
    std_out: bytes
    std_err: bytes


class WinRMSession(typing.Protocol):
    def run_ps(self, script: str) -> _WinRMResponse: ...


@dataclasses.dataclass
class _NativeResponse:
    status_code: int
    std_out: bytes
    std_err: bytes


class NativeWinRMSession:
    """Current-context Windows PowerShell remoting session adapter."""

    def __init__(
        self,
        host: str,
        *,
        ssl: bool = False,
        port: typing.Optional[int] = None,
        timeout: typing.Optional[float] = None,
    ) -> None:
        self.host = host
        self.ssl = ssl
        self.port = port
        self.timeout = timeout

    def run_ps(self, script: str) -> _NativeResponse:
        host = base64.b64encode(self.host.encode("utf-8")).decode("ascii")
        payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
        wrapper = (
            "$OutputEncoding=[Console]::OutputEncoding="
            "[Text.UTF8Encoding]::new($false);"
            "$ErrorActionPreference='Stop';"
            "$h=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{host}'));"
            "$s=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{payload}'));"
            "$o=@{ComputerName=$h;Authentication='Negotiate';"
            f"UseSSL=${str(self.ssl).lower()}"
            + (f";Port={self.port}" if self.port is not None else "")
            + "};"
            "try{Invoke-Command @o -ScriptBlock ([ScriptBlock]::Create($s))}"
            "catch{$c=[string]$_.CategoryInfo.Category;"
            "$m=[Convert]::ToBase64String("
            "[Text.Encoding]::UTF8.GetBytes($_.Exception.Message));"
            "[Console]::Error.Write('HOSTCTL_NATIVE_ERROR:'+$c+':'+$m);exit 1}"
        )
        try:
            result = subprocess.run(
                (
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "-",
                ),
                input=wrapper.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise
        marker = b"HOSTCTL_NATIVE_ERROR:"
        if result.returncode and result.stderr.startswith(marker):
            _, category, encoded = result.stderr.decode("ascii").split(":", 2)
            message = base64.b64decode(encoded).decode("utf-8", "replace")
            if category in ("AuthenticationError", "PermissionDenied"):
                raise PermissionError(message)
            raise ConnectionError(message)
        return _NativeResponse(result.returncode, result.stdout, result.stderr)

    def close(self) -> None:
        return None


class WinRMExecutor(Executor[subprocess.CompletedProcess]):
    """Execute finalized PowerShell scripts through a pywinrm session."""

    executor_capabilities = frozenset()

    def __init__(
        self,
        session: typing.Callable[[], WinRMSession],
        transport_timeout: typing.Optional[typing.Callable[[], float]] = None,
    ) -> None:
        self._session = session
        self._transport_timeout = transport_timeout

    def __call__(
        self,
        command: ExecutorCommand,
        *args: CommandArgument,
        bufsize: int = -1,
        stdin: typing.Optional[FileHandle] = None,
        stdout: typing.Optional[FileHandle] = None,
        stderr: typing.Optional[FileHandle] = None,
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
            raise TypeError(f"unsupported WinRM executor option: {sorted(options)[0]}")
        if args:
            raise NotImplementedError("WinRMExecutor does not support native arguments")
        command = str(command)
        if timeout is not None:
            raise NotImplementedError(
                "WinRMExecutor timeout is unsupported; configure transport timeouts"
            )
        if stdin is not None or input is not None:
            raise NotImplementedError("WinRMExecutor does not support stdin/input")
        stdout, stderr = capture_streams(capture_output, stdout, stderr)
        try:
            result = self._session().run_ps(command)
        except Exception as exc:
            normalized = self._normalize_error(exc, command)
            if normalized is exc:
                raise
            raise normalized from exc
        out = result.std_out
        err = result.std_err
        if text or encoding is not None or errors is not None:
            codec = encoding or "utf-8"
            out = out.decode(codec, errors or "strict")
            err = err.decode(codec, errors or "strict")

        if stderr is subprocess.STDOUT:
            if out is not None:
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
            args=command,
            returncode=result.status_code,
            stdout=out,
            stderr=err,
        )
        if check:
            completed.check_returncode()
        return completed

    def _normalize_error(self, exc: Exception, command: str) -> Exception:
        try:
            import requests
            import winrm.exceptions
        except ImportError:
            return exc
        if isinstance(exc, winrm.exceptions.AuthenticationError):
            return PermissionError(str(exc))
        if isinstance(
            exc,
            (
                TimeoutError,
                subprocess.TimeoutExpired,
                requests.exceptions.Timeout,
                winrm.exceptions.WinRMOperationTimeoutError,
            ),
        ):
            timeout = self._transport_timeout() if self._transport_timeout else None
            return subprocess.TimeoutExpired(command, timeout)
        if isinstance(
            exc,
            (
                requests.exceptions.ConnectionError,
                winrm.exceptions.WinRMTransportError,
                winrm.exceptions.WSManFaultError,
            ),
        ):
            return ConnectionError(str(exc))
        return exc
