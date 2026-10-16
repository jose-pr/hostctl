"""WinRM PowerShell script executor."""

from __future__ import annotations

import base64
import dataclasses
import subprocess
import uuid
import typing

from ...executor import (
    POWERSHELL_STATUS,
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
class _Response:
    status_code: int
    std_out: bytes
    std_err: bytes


#: Emitted by the remote script block and consumed by the local wrapper.
#: `Invoke-Command` does not copy the remote `$LASTEXITCODE` into the calling
#: session, so a native command that fails only by exit code would otherwise
#: report success locally.  Same convention as the PSRP runspace session.
NATIVE_EXIT_MARKER = "__HOSTCTL_LASTEXITCODE__"


def exit_marker() -> str:
    """A marker no remote output can forge by accident.

    The fixed `__HOSTCTL_LASTEXITCODE__` is a string any command may print --
    `grep`ping the sources, echoing a log line -- and the wrapper removed
    that line from stdout and took the number after it as the exit status.
    A per-call nonce makes a collision require the caller's own output to
    contain a value it cannot know.
    """
    return f"{NATIVE_EXIT_MARKER[:-2]}_{uuid.uuid4().hex}__"


#: Clears a status left by an earlier native command in a persistent session.
LASTEXITCODE_RESET = "$global:LASTEXITCODE=0"


def status_epilogue(marker: str) -> str:
    """Report `POWERSHELL_STATUS` as a `marker:N` output line.

    Appended on its own line to the remote script, so the payload's last
    statement -- comment, `}` or bare expression alike -- cannot swallow it.
    """
    return f"Write-Output ('{marker}:' + {POWERSHELL_STATUS})"


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


#: Largest single Send to the remote shell: 64 KiB of script is about 87 KB as
#: base64, under the smallest default `MaxEnvelopeSizekb` (150).
_STDIN_CHUNK = 64 * 1024

_PS_ARGV = ("-NoProfile", "-NonInteractive", "-Command", "-")


class _PywinrmStdinSession:
    """Run a script through pywinrm's `Protocol`, with the script on stdin.

    `Session.run_ps` puts the script on a `powershell -encodedcommand` line
    that cmd.exe parses and caps near 8 KB. Here PowerShell starts directly
    (`skip_cmd_shell`) and reads one ASCII bootstrap line from stdin, so no
    command line bounds the script. `-Command -` writes plain text to stderr,
    so there is no CLIXML to strip.
    """

    def __init__(self, protocol: typing.Any) -> None:
        self._protocol = protocol

    def run_ps(self, script: str) -> _Response:
        protocol = self._protocol
        bootstrap = (
            "$s=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{_b64(script)}'));"
            ". ([ScriptBlock]::Create($s))\r\n"
        ).encode("ascii")
        shell = protocol.open_shell()
        command_id = None
        try:
            command_id = protocol.run_command(
                shell,
                "powershell.exe",
                list(_PS_ARGV),
                console_mode_stdin=False,
                skip_cmd_shell=True,
            )
            last = len(bootstrap) - 1
            for start in range(0, len(bootstrap), _STDIN_CHUNK):
                protocol.send_command_input(
                    shell,
                    command_id,
                    bootstrap[start : start + _STDIN_CHUNK],
                    end=start + _STDIN_CHUNK > last,
                )
            out, err, status = protocol.get_command_output(shell, command_id)
        finally:
            try:
                if command_id is not None:
                    protocol.cleanup_command(shell, command_id)
            finally:
                protocol.close_shell(shell)
        return _Response(status, out, err)

    def close(self) -> None:
        return None


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

    def _wrapper(self, script: str, marker: typing.Optional[str] = None) -> str:
        """Build the local PowerShell program that performs one remote call.

        Everything variable is base64 -- the host, the payload, the status
        epilogue and the marker -- so no caller-supplied text is ever spliced
        into PowerShell source.  The connection options are assembled as a
        list rather than patched into rendered text afterwards: the previous
        `str.replace` approach silently did nothing without a port and
        produced a hashtable `Invoke-Command` rejects with one.

        Remote output streams through one pipeline. Error records from the
        remote side go to stderr; a transport failure (the local session
        itself could not run the call) is reported as a final
        `MARKER:error:KIND:MESSAGE` stderr line instead.
        """
        marker = marker or exit_marker()
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

        def decoded(name: str, value: str) -> str:
            return (
                f"${name}=[Text.Encoding]::UTF8.GetString("
                f"[Convert]::FromBase64String('{_b64(value)}'));"
            )

        relay = (
            "ForEach-Object{"
            "if($_ -is [Management.Automation.ErrorRecord]){"
            "if($_.Exception -is "
            "[Management.Automation.Remoting.PSRemotingTransportException])"
            "{$tx=$_}else{Write-Error -ErrorRecord $_}}"
            "elseif(([string]$_).StartsWith($m))"
            "{$c=[int](([string]$_).Split(':')[1])}"
            "else{$_}}"
        )
        # A transport failure is authentication when the documented remoting
        # ids or Win32 codes (ERROR_ACCESS_DENIED, ERROR_LOGON_FAILURE) say so.
        report = (
            "if($tx){$k='transport';$i=[string]$tx.FullyQualifiedErrorId;"
            "$n=0;try{$n=[int]$tx.Exception.ErrorCode}catch{};"
            "if($i.StartsWith('AccessDenied') -or $i.StartsWith('LogonFailure') "
            "-or $n -eq 5 -or $n -eq 1326){$k='auth'};"
            "[Console]::Error.WriteLine($m+'error:'+$k+':'+"
            "[Convert]::ToBase64String("
            "[Text.Encoding]::UTF8.GetBytes($tx.Exception.Message)));exit 1};"
            "exit $c"
        )
        return (
            "$OutputEncoding=[Console]::OutputEncoding="
            "[Text.UTF8Encoding]::new($false);"
            "$ErrorActionPreference='Stop';"
            + decoded("h", self.host)
            + decoded("s", script)
            + decoded("e", status_epilogue(marker))
            + decoded("m", marker + ":")
            + prelude
            + "$o=@{"
            + ";".join(options)
            + "};$c=1;$tx=$null;"
            + f"try{{{LASTEXITCODE_RESET};"
            "$b=[ScriptBlock]::Create($s+[Environment]::NewLine+$e);"
            "$ErrorActionPreference='Continue';"
            "Invoke-Command @o -ScriptBlock $b -ErrorAction Continue 2>&1 | "
            + relay
            + ";"
            + report
            + "}catch{"
            # PowerShell 7 raises a remote `throw` locally as a RemoteException;
            # that is the command failing, not the transport.
            "if($_.Exception -is [Management.Automation.RemoteException])"
            "{[Console]::Error.WriteLine([string]$_.Exception.Message);exit 1};"
            "[Console]::Error.WriteLine($m+'error:transport:'+"
            "[Convert]::ToBase64String("
            "[Text.Encoding]::UTF8.GetBytes($_.Exception.Message)));exit 1}"
        )

    def run_ps(
        self, script: str, *, timeout: typing.Optional[float] = None
    ) -> _Response:
        """Run one script; `timeout` overrides the session deadline per call."""
        marker = exit_marker()
        wrapper = self._wrapper(script, marker)
        deadline = timeout if timeout is not None else self.timeout
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
                timeout=deadline,
            )
        except subprocess.TimeoutExpired as exc:
            # Never expose the local powershell argv as the remote command.
            # The remote command keeps running: WinRS has no cancel.
            raise expired(self.host, deadline, orphaned=True) from exc
        # Only the wrapper's own last stderr line can carry this call's marker;
        # remote text that merely resembles it is returned as stderr.
        lines = [line for line in result.stderr.splitlines() if line.strip()]
        prefix = f"{marker}:error:".encode("ascii")
        if lines and lines[-1].startswith(prefix):
            try:
                _, _, kind, encoded = lines[-1].decode("ascii").strip().split(":", 3)
                detail = base64.b64decode(encoded).decode("utf-8", "replace")
            except Exception:
                kind, detail = "transport", result.stderr.decode("utf-8", "replace")
            if kind == "auth":
                raise PermissionError(detail)
            raise ConnectionError(detail)
        return _Response(result.returncode, result.stdout, result.stderr)

    def close(self) -> None:
        return None


class WinRMExecutor(Executor[subprocess.CompletedProcess]):
    """Execute finalized PowerShell scripts through a WinRM session.

    `manages_status` declares that the session reports the remote status
    itself, so the flavour's own `exit` epilogue must stay off the script.
    `accepts_timeout` lets `run(timeout=)` through as the session's per-call
    deadline; without it a `timeout=` is refused.
    """

    executor_capabilities = frozenset((ExecutorCapability.SCRIPT,))

    def __init__(
        self,
        session: typing.Callable[[], WinRMSession],
        transport_timeout: typing.Optional[typing.Callable[[], float]] = None,
        *,
        manages_status: bool = False,
        accepts_timeout: bool = False,
    ) -> None:
        self._session = session
        self._transport_timeout = transport_timeout
        self._accepts_timeout = accepts_timeout
        capabilities = {ExecutorCapability.SCRIPT}
        if manages_status:
            capabilities.add(ExecutorCapability.MANAGES_STATUS)
        self.executor_capabilities = frozenset(capabilities)

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
        if timeout is not None and not self._accepts_timeout:
            raise NotImplementedError(
                "WinRMExecutor timeout is unsupported; configure transport timeouts"
            )
        if stdin is not None or input is not None:
            raise NotImplementedError("WinRMExecutor does not support stdin/input")
        stdout, stderr = capture_streams(capture_output, stdout, stderr)
        try:
            session = self._session()
            if timeout is None:
                result = session.run_ps(command)
            else:
                result = session.run_ps(command, timeout=timeout)  # type: ignore[call-arg]
        except Exception as exc:
            normalized = self._normalize_error(exc, command, timeout)
            if normalized is exc:
                raise
            raise normalized from exc
        out = result.std_out
        err = result.std_err
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

    def _normalize_error(
        self, exc: Exception, command: str, timeout: typing.Optional[float] = None
    ) -> Exception:
        # The dependency-free cases FIRST. Returning early on the optional
        # import also disabled this branch, so on a native-only install --
        # the documented password-free Windows path, which needs no winrm
        # extra -- a timeout crossed the boundary as a builtin `TimeoutError`
        # and `except subprocess.TimeoutExpired` did not catch it.
        if timeout is None and self._transport_timeout:
            timeout = self._transport_timeout()
        if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
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
