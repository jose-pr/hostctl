"""WinRM PowerShell script executor."""

from __future__ import annotations

import base64
import dataclasses
import subprocess
import typing

from ._common import (
    expired,
    CaptureOutput,
    command_text,
    CommandArgument,
    Executor,
    ExecutorCommand,
    ExecutorCapability,
    FileHandle,
    Input,
    dispatch_output,
    capture_streams,
    wants_text,
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


#: Emitted by the remote script block and consumed by the local wrapper.
#: `Invoke-Command` does not copy the remote `$LASTEXITCODE` into the calling
#: session, so a native command that fails only by exit code would otherwise
#: report success locally.  Same convention as the PSRP runspace session.
NATIVE_EXIT_MARKER = "__HOSTCTL_LASTEXITCODE__"

#: Runs *inside* the remote script block, appended on its own line so that the
#: payload's last statement -- comment, `}` or bare expression alike -- cannot
#: swallow it.
_NATIVE_EXIT_EPILOGUE = (
    f"Write-Output ('{NATIVE_EXIT_MARKER}:' + [string]([int]$LASTEXITCODE))"
)


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


class NativeWinRMSession:
    """Current-context Windows PowerShell remoting session adapter."""

    def __init__(
        self,
        host: str,
        *,
        ssl: bool = False,
        port: typing.Optional[int] = None,
        timeout: typing.Optional[float] = None,
        transport: str = "ntlm",
        server_cert_validation: str = "validate",
        message_encryption: str = "auto",
    ) -> None:
        self.host = host
        self.ssl = ssl
        self.port = port
        self.timeout = timeout
        if transport not in {"ntlm", "kerberos"}:
            raise NotImplementedError(
                "native WinRM supports only current-context Negotiate (ntlm/kerberos)"
            )
        if message_encryption not in {"auto", "always", "never"}:
            raise ValueError("invalid message_encryption")
        if message_encryption != "auto":
            raise NotImplementedError(
                "native WinRM cannot override WS-Man message encryption; "
                "use message_encryption='auto' or the pywinrm provider"
            )
        if server_cert_validation not in {"validate", "ignore"}:
            raise ValueError("invalid server_cert_validation")
        if ssl and server_cert_validation == "ignore":
            # PowerShell remoting can skip certificate checks, but only when
            # explicitly requested; keep this visible in the generated options.
            self._skip_ca_check = True
        else:
            self._skip_ca_check = False

    def _wrapper(self, script: str) -> str:
        """Build the local PowerShell program that performs one remote call.

        Everything variable is base64 -- the host, the payload, and the exit
        epilogue -- so no caller-supplied text is ever spliced into PowerShell
        source.  The connection options are assembled as a list rather than
        patched into rendered text afterwards: the previous `str.replace`
        approach silently did nothing without a port and produced a hashtable
        `Invoke-Command` rejects with one.
        """
        options = [
            "ComputerName=$h",
            "Authentication='Negotiate'",
            f"UseSSL=${str(self.ssl).lower()}",
        ]
        if self.port is not None:
            options.append(f"Port={self.port}")
        prelude = ""
        if self._skip_ca_check:
            # `SkipCACheck` is a `New-PSSessionOption` parameter, not an
            # `Invoke-Command` one.  Both checks are skipped together to match
            # pywinrm's `server_cert_validation="ignore"`.
            prelude = "$so=New-PSSessionOption -SkipCACheck -SkipCNCheck;"
            options.append("SessionOption=$so")
        return (
            "$OutputEncoding=[Console]::OutputEncoding="
            "[Text.UTF8Encoding]::new($false);"
            "$ErrorActionPreference='Stop';"
            "$h=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{_b64(self.host)}'));"
            "$s=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{_b64(script)}'));"
            "$e=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{_b64(_NATIVE_EXIT_EPILOGUE)}'));"
            + prelude
            + "$o=@{"
            + ";".join(options)
            + "};"
            "try{$global:LASTEXITCODE=0;"
            "$b=[ScriptBlock]::Create($s+[Environment]::NewLine+$e);"
            "$r=Invoke-Command @o -ScriptBlock $b;"
            "$c=[int]$global:LASTEXITCODE;$out=@();"
            "foreach($v in $r){$t=[string]$v;"
            f"if($t.StartsWith('{NATIVE_EXIT_MARKER}:'))"
            "{$c=[int]($t.Split(':')[1])}else{$out+=$v}};"
            "$out;exit $c}"
            "catch{$c=[string]$_.CategoryInfo.Category;"
            "$m=[Convert]::ToBase64String("
            "[Text.Encoding]::UTF8.GetBytes($_.Exception.Message));"
            "[Console]::Error.Write('HOSTCTL_NATIVE_ERROR:'+$c+':'+$m);exit 1}"
        )

    def run_ps(self, script: str) -> _NativeResponse:
        wrapper = self._wrapper(script)
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
        except subprocess.TimeoutExpired as exc:
            # Never expose the local powershell argv as the remote command.
            # The remote command keeps running: WinRS has no cancel.
            raise expired(self.host, self.timeout, orphaned=True) from exc
        return _NativeResponse(result.returncode, result.stdout, result.stderr)

    def close(self) -> None:
        return None


class WinRMExecutor(Executor[subprocess.CompletedProcess]):
    """Execute finalized PowerShell scripts through a pywinrm session."""

    executor_capabilities = frozenset((ExecutorCapability.SCRIPT,))

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
        command = command_text(command)
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
        marker = b"HOSTCTL_NATIVE_ERROR:"
        if result.status_code and err.startswith(marker):
            try:
                _, category, encoded = err.decode("ascii").split(":", 2)
                detail = base64.b64decode(encoded).decode("utf-8", "replace")
            except Exception:
                category, detail = "transport", err.decode("utf-8", "replace")
            if category in ("AuthenticationError", "PermissionDenied"):
                raise PermissionError(detail)
            if category not in ("RemoteError", "Remote"):
                raise ConnectionError(detail)
            # Remote errors are represented as a normal non-zero completion;
            # check=False must be able to inspect them.
            err = detail.encode(encoding or "utf-8", errors or "replace")
        if wants_text(text, encoding, errors):
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
        # The dependency-free cases FIRST. Returning early on the optional
        # import also disabled this branch, so on a native-only install --
        # the documented password-free Windows path, which needs no winrm
        # extra -- a timeout crossed the boundary as a builtin `TimeoutError`
        # and `except subprocess.TimeoutExpired` did not catch it.
        if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
            timeout = self._transport_timeout() if self._transport_timeout else None
            return expired(command, timeout, orphaned=True)
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
                requests.exceptions.Timeout,
                winrm.exceptions.WinRMOperationTimeoutError,
            ),
        ):
            timeout = self._transport_timeout() if self._transport_timeout else None
            # WinRS has no cancel, so the remote command keeps running.
            return expired(command, timeout, orphaned=True)
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
