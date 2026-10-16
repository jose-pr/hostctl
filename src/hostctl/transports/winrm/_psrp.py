"""PowerShell Remoting: the PSRP executor and typed runspace sessions."""

from __future__ import annotations

import importlib
import subprocess
import typing
import types

from ...executor import (
    CaptureOutput,
    command_text,
    CommandArgument,
    Executor,
    ExecutorCapability,
    ExecutorCommand,
    FileHandle,
    Input,
    capture_streams,
    dispatch_output,
    wants_text,
)

if typing.TYPE_CHECKING:
    pass

import dataclasses
import threading


def pypsrp_available() -> bool:
    """Return whether the optional PSRP dependency can be imported."""
    if __import__("sys").version_info < (3, 10):
        return False
    try:
        importlib.import_module("pypsrp")
    except ImportError:
        return False
    return True


def require_pypsrp() -> types.ModuleType:
    """Import pypsrp with an actionable, version-aware error."""
    import sys

    if sys.version_info < (3, 10):
        raise ImportError(
            "PSRP support requires Python 3.10 or newer; use the pywinrm "
            "provider on Python 3.9"
        )
    try:
        return importlib.import_module("pypsrp")
    except ImportError as exc:
        raise ImportError(
            "PSRP support requires the 'psrp' extra: " "pip install hostctl[psrp]"
        ) from exc


class PSRPExecutor(Executor[subprocess.CompletedProcess]):
    """Execute finalized PowerShell scripts in fresh PSRP pipelines."""

    # PSRP receives a finalized PowerShell script; cwd/env are embedded by
    # the host shell rather than forwarded as unsupported executor options.
    executor_capabilities = frozenset(
        (ExecutorCapability.SCRIPT, ExecutorCapability.MANAGES_STATUS)
    )

    def __init__(self, session: typing.Callable[[], RunspaceSession]) -> None:
        self._session = session

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
        del bufsize
        if options:
            raise TypeError(f"unsupported PSRP executor option: {sorted(options)[0]}")
        if args:
            raise NotImplementedError("PSRP executor receives a finalized script")
        if stdin is not None or input is not None:
            raise NotImplementedError("PSRP does not provide raw stdin for run()")
        if timeout is not None:
            raise NotImplementedError(
                "PSRP pipeline timeout is unsupported; configure WinRM timeouts"
            )
        stdout, stderr = capture_streams(capture_output, stdout, stderr)
        result = self._session().invoke(
            command_text(command), raw=False, capture_exit=True, format_text=True
        )
        codec = encoding or "utf-8"
        # `invoke(capture_exit=True)` already consumed the
        # `__HOSTCTL_LASTEXITCODE__` line and folded it into `returncode`;
        # the session is the only parser of that marker.
        returncode = result.returncode
        # Output is already console text (`format_text`), one item per line;
        # the other legs deliver CRLF, so this one does too.
        out_text = "\r\n".join(str(item) for item in result.output)
        err_text = "\r\n".join(str(item) for item in result.streams.error)
        if out_text:
            out_text += "\r\n"
        as_text = wants_text(text, encoding, errors)
        out: typing.Union[str, bytes] = out_text if as_text else out_text.encode(codec)
        err: typing.Union[str, bytes] = err_text if as_text else err_text.encode(codec)
        if stderr is subprocess.STDOUT:
            out = out + err
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
            args=command_text(command),
            returncode=returncode,
            stdout=out,
            stderr=err,
        )
        if check:
            completed.check_returncode()
        return completed


@dataclasses.dataclass(frozen=True)
class PipelineStreams:
    """Typed PowerShell streams returned by one pipeline invocation."""

    error: tuple[object, ...] = ()
    warning: tuple[object, ...] = ()
    verbose: tuple[object, ...] = ()
    debug: tuple[object, ...] = ()
    information: tuple[object, ...] = ()
    progress: tuple[object, ...] = ()


@dataclasses.dataclass(frozen=True)
class PipelineResult:
    """Result of a PSRP pipeline, retaining objects and stream records."""

    output: tuple[object, ...]
    streams: PipelineStreams
    state: str
    had_errors: bool
    returncode: int


def _stream_values(streams: object, name: str) -> tuple[object, ...]:
    value = getattr(streams, name, ())
    try:
        return tuple(value)
    except TypeError:
        return (value,) if value is not None else ()


def _pipeline_state_name(value: object) -> str:
    """Normalize pypsrp's integer states and compatible enum-like doubles."""
    value = getattr(value, "name", getattr(value, "state", value))
    if isinstance(value, int):
        return {
            0: "NotStarted",
            1: "Running",
            2: "Stopping",
            3: "Stopped",
            4: "Completed",
            5: "Failed",
            6: "Disconnected",
        }.get(value, f"Unknown({value})")
    return str(value)


#: `WinRMConfig.transport` spelled the way pypsrp's WSMan names it.
#:
#: The pool authenticates with the transport the config names, as pywinrm
#: does, so a config behaves the same with or without the psrp extra.
#: `plaintext` is pywinrm's name for basic over http, and `ssl` is its name
#: for basic over https: certificate auth needs a client certificate, which
#: `WinRMConfig` does not carry.
_PSRP_AUTH = {
    "basic": "basic",
    "plaintext": "basic",
    "credssp": "credssp",
    "kerberos": "kerberos",
    "ntlm": "ntlm",
    "ssl": "basic",
}


class RunspaceSession:
    """A persistent PSRP runspace with typed PowerShell pipeline streams.

    ``RunspaceSession`` intentionally does not implement the byte-oriented
    ``Process``/``ShellSession`` contract: a runspace is a PowerShell object
    pipeline, not a TTY and has no raw stdin or resize operations.
    """

    def __init__(
        self, config: object | None = None, *, pool: object | None = None
    ) -> None:
        self._config = config
        self._pool = pool
        self._owns_pool = pool is None
        self._open = False
        #: Whether this session has ever opened its pool; see `connect`.
        self._opened = False
        #: Guards `connect()`/`close()`: a provider owns its own locking, and
        #: two threads on a fresh session each built and opened a pool.
        self._lifecycle = threading.RLock()

    def _make_pool(self) -> object:
        if self._pool is not None:
            return self._pool
        require_pypsrp()
        from pypsrp.powershell import RunspacePool
        from netimps import get_default_port
        from pypsrp.wsman import WSMan

        config = self._config
        if config is None:
            raise ValueError("a WinRMConfig is required when pool is not supplied")
        ssl = bool(getattr(config, "ssl", False))
        endpoint_port = getattr(config, "port", None)
        auth = _PSRP_AUTH.get(
            str(getattr(config, "transport", "") or "").casefold(), "negotiate"
        )
        encryption = getattr(config, "message_encryption", "auto")
        if auth == "basic" and not ssl and getattr(config, "allow_cleartext", False):
            # pypsrp refuses basic over http unless message encryption is
            # off; the caller opted in with `allow_cleartext`.
            encryption = "never"
        kwargs = {
            "server": getattr(config, "host"),
            "port": endpoint_port or get_default_port("winrms" if ssl else "winrm"),
            "username": getattr(config, "username"),
            "password": getattr(config, "password", None),
            "ssl": ssl,
            "auth": auth,
            "cert_validation": getattr(config, "server_cert_validation", "validate")
            == "validate",
            "encryption": encryption,
            "connection_timeout": getattr(config, "read_timeout_sec", 30),
            "operation_timeout": getattr(config, "operation_timeout_sec", 20),
            "read_timeout": getattr(config, "read_timeout_sec", 30),
        }
        return RunspacePool(WSMan(**kwargs))

    def connect(self) -> None:
        # One lock: two threads calling `connect()` on a fresh session each
        # built a pool, and the loser's pool was opened and then dropped --
        # an authenticated connection nothing closes.
        with self._lifecycle:
            if self._open:
                return
            if self._pool is None:
                self._pool = self._make_pool()
            open_method = getattr(self._pool, "open", None)
            # An injected pool is opened ONCE, on the first connect. Opening
            # it again after a `close()` -- which deliberately leaves an
            # injected pool alone -- reset a pool the caller was still using
            # elsewhere, which is the reason to inject one at all.
            if open_method is not None and (self._owns_pool or not self._opened):
                open_method()
            self._opened = True
            self._open = True

    def close(self) -> None:
        with self._lifecycle:
            self._close_locked()

    def _close_locked(self) -> None:
        if not self._open and self._pool is None:
            return
        pool = self._pool
        self._open = False
        if not self._owns_pool:
            # An injected pool belongs to whoever supplied it. Closing it
            # here would take it out from under a caller sharing one pool
            # across sessions, which is the reason to inject one at all.
            # Keeping the reference also leaves the session reopenable.
            return
        self._pool = None
        if pool is not None:
            close = getattr(pool, "close", None)
            if close is not None:
                close()

    def __enter__(self) -> RunspaceSession:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[types.TracebackType],
    ) -> bool:
        self.close()
        return False

    def invoke(
        self,
        script: object,
        *args: object,
        raw: bool = False,
        capture_exit: bool = False,
        format_text: bool = False,
    ) -> PipelineResult:
        """Invoke a script or executable with persistent runspace state.

        `format_text` renders the output through `Out-String -Stream` inside
        the runspace, so it is the text a console would show (tables
        included) rather than each object's `ToString()`.
        """
        self.connect()
        from ...shell import POWERSHELL

        if args:
            # The flavour's own renderer: a `PSParam` renders as a bare
            # parameter name, which the binder needs, and native arguments
            # get the C-runtime escaping every other leg applies.
            script_text = POWERSHELL.structured_command((script, *args))
        else:
            script_text = str(script)
        from ._native import LASTEXITCODE_RESET, exit_marker, status_epilogue

        # A per-call nonce, for the same reason the native wrapper uses one:
        # the fixed marker is a string any command may print, and the line
        # after it was removed from stdout and read as the exit status.
        marker = exit_marker()
        if capture_exit:
            # Reset first: this runspace is persistent, so `$LASTEXITCODE`
            # survives from one pipeline to the next, and a pure-PowerShell
            # command, which never sets it, would report the stale status of
            # an earlier *native* command.
            # The epilogue goes on its OWN LINE: joined with `;` after the
            # payload, a script ending in a `#` comment commented it out,
            # and the pipeline then reported whatever status the previous
            # one had left behind.
            script_text = (
                f"{LASTEXITCODE_RESET}; {script_text}" + "\n" + status_epilogue(marker)
            )
        if format_text:
            # Dot-sourced, so runspace state persists and the status epilogue
            # still sees the payload's last statement.
            script_text = ". {\n" + script_text + "\n} | Out-String -Stream"
        from pypsrp.powershell import PowerShell

        pipeline = PowerShell(self._pool)
        pipeline.add_script(script_text)
        output = pipeline.invoke()
        streams_obj = getattr(pipeline, "streams", object())
        streams = PipelineStreams(
            error=_stream_values(streams_obj, "error"),
            warning=_stream_values(streams_obj, "warning"),
            verbose=_stream_values(streams_obj, "verbose"),
            debug=_stream_values(streams_obj, "debug"),
            information=_stream_values(streams_obj, "information"),
            progress=_stream_values(streams_obj, "progress"),
        )
        state = _pipeline_state_name(getattr(pipeline, "state", "Completed"))
        had_errors = bool(getattr(pipeline, "had_errors", bool(streams.error)))
        values = tuple(output or ())
        returncode = 0 if state.casefold() == "completed" and not had_errors else 1
        if capture_exit:
            retained = []
            for value in values:
                text_value = str(value)
                if text_value.startswith(marker + ":"):
                    try:
                        # The status the script itself computed; a
                        # non-terminating error earlier in the pipeline does
                        # not override it.
                        returncode = int(text_value.split(":", 1)[1])
                    except ValueError:
                        had_errors = True
                        returncode = 1
                else:
                    retained.append(value)
            values = tuple(retained)
        if raw:
            values = tuple(values)
        else:
            values = tuple(str(value) for value in values)
        return PipelineResult(values, streams, state, had_errors, returncode)
