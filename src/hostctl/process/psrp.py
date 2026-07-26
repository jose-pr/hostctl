"""Persistent PowerShell Remoting Protocol runspace sessions."""

from __future__ import annotations

import dataclasses
import types
import typing

from ..executor.psrp import require_pypsrp


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

    def _make_pool(self) -> object:
        if self._pool is not None:
            return self._pool
        require_pypsrp()
        from pypsrp.powershell import RunspacePool
        from pypsrp.wsman import WSMan

        config = self._config
        if config is None:
            raise ValueError("a WinRMConfig is required when pool is not supplied")
        ssl = bool(getattr(config, "ssl", False))
        endpoint_port = getattr(config, "port", None)
        kwargs = {
            "server": getattr(config, "host"),
            "port": endpoint_port or (5986 if ssl else 5985),
            "username": getattr(config, "username"),
            "password": getattr(config, "password", None),
            "ssl": ssl,
            "auth": "negotiate",
            "cert_validation": getattr(config, "server_cert_validation", "validate")
            == "validate",
            "encryption": getattr(config, "message_encryption", "auto"),
            "connection_timeout": getattr(config, "read_timeout_sec", 30),
            "operation_timeout": getattr(config, "operation_timeout_sec", 20),
            "read_timeout": getattr(config, "read_timeout_sec", 30),
        }
        try:
            wsman = WSMan(**kwargs)
        except TypeError:
            # pypsrp releases have renamed timeout parameters; retry with the
            # stable connection options and let transport errors surface.
            kwargs.pop("operation_timeout")
            kwargs.pop("read_timeout")
            wsman = WSMan(**kwargs)
        return RunspacePool(wsman)

    def connect(self) -> None:
        if self._open:
            return
        self._pool = self._make_pool()
        open_method = getattr(self._pool, "open", None)
        if open_method is not None:
            open_method()
        self._open = True

    def close(self) -> None:
        if not self._open and self._pool is None:
            return
        pool, self._pool = self._pool, None
        self._open = False
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
    ) -> PipelineResult:
        """Invoke a script or executable with persistent runspace state."""
        self.connect()
        if args:
            # PowerShell's call operator preserves executable semantics while
            # still allowing arguments to be represented as object literals.
            command = str(script).replace("'", "''")
            values = " ".join(
                "'{}'".format(str(value).replace("'", "''")) for value in args
            )
            script_text = "& '{}' {}".format(command, values)
        else:
            script_text = str(script)
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
        state_obj = getattr(pipeline, "state", "Completed")
        state = getattr(state_obj, "name", state_obj)
        state = str(state)
        had_errors = bool(getattr(pipeline, "had_errors", bool(streams.error)))
        returncode = 0 if state.casefold() == "completed" and not had_errors else 1
        if raw:
            values = tuple(output or ())
        else:
            values = tuple(str(value) for value in (output or ()))
        return PipelineResult(values, streams, state, had_errors, returncode)
