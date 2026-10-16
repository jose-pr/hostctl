"""Native Windows WinRM through the local PowerShell `Invoke-Command`."""

from __future__ import annotations

import base64
import math
import subprocess
import uuid
import typing

from ..._windows import no_window_flags
from ...executor import POWERSHELL_STATUS, expired
from ._pywinrm import _Response, _b64

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


#: `New-PSSessionOption` takes milliseconds as an `Int32`.
_MAX_SESSION_OPTION_MS = 2**31 - 1


class NativeWinRMSession:
    """Current-context Windows PowerShell remoting session adapter.

    `timeout` is the total deadline of one call (seconds, `None` for none);
    `open_timeout` bounds establishing the remote session, as
    `New-PSSessionOption -OpenTimeout` (seconds, `None` for PowerShell's 180).
    """

    def __init__(
        self,
        host: str,
        *,
        ssl: bool = False,
        port: typing.Optional[int] = None,
        timeout: typing.Optional[float] = None,
        open_timeout: typing.Optional[float] = None,
        transport: str = "ntlm",
        verify: bool = True,
        message_encryption: str = "auto",
    ) -> None:
        self.host = host
        self.ssl = ssl
        self.port = port
        self.timeout = timeout
        self.open_timeout = open_timeout
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
        if ssl and not verify:
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
        # `SkipCACheck` and `OpenTimeout` are `New-PSSessionOption`
        # parameters, not `Invoke-Command` ones.  Both certificate checks are
        # skipped together to match the pywinrm leg's `verify=False`.
        session_options = []
        if self._skip_ca_check:
            session_options.append("-SkipCACheck -SkipCNCheck")
        if self.open_timeout is not None:
            milliseconds = min(
                math.ceil(self.open_timeout * 1000), _MAX_SESSION_OPTION_MS
            )
            session_options.append(f"-OpenTimeout {milliseconds}")
        prelude = ""
        if session_options:
            prelude = "$so=New-PSSessionOption " + " ".join(session_options) + ";"
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
                creationflags=no_window_flags(),
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
