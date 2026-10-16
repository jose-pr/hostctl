"""pywinrm-backed WinRS execution."""

from __future__ import annotations

import base64
import dataclasses
import subprocess
import typing

from ...executor import (
    expired,
    CaptureOutput,
    command_text,
    CommandArgument,
    Executor,
    CommandLike,
    ExecutorCapability,
    StreamTarget,
    CommandInput,
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
        command: CommandLike,
        *args: CommandArgument,
        bufsize: int = -1,
        stdin: typing.Optional[StreamTarget] = None,
        stdout: typing.Optional[StreamTarget] = None,
        stderr: typing.Optional[StreamTarget] = None,
        capture_output: CaptureOutput = True,
        check: bool = True,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
        input: CommandInput = None,
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
