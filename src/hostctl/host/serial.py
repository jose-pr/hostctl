"""Serial-console host and opaque serial connection configuration."""

from __future__ import annotations

import dataclasses
import subprocess
import time
import typing
from urllib.parse import quote, unquote, urlencode

from ..executor import (
    CaptureOutput,
    Environment,
    FileHandle,
    Input,
    SerialExecutor,
    SerialFactory,
    SerialLike,
    SerialSettings,
    capture_streams,
    dispatch_output,
    expired,
    wants_text,
)
from ..process import Process, SerialConsoleProcess, terminal_options
from ..serial import ConsoleProtocolError, RawConsoleProfile, SerialConsoleProtocol
from ._common import (
    Command,
    Host,
    HostConfig,
    HostInfo,
    PathLike,
    starts_direct_command,
    strict_uri_query,
)


def _bool(value: str, name: str) -> bool:
    normalized = value.casefold()
    if normalized not in ("true", "false"):
        raise ValueError(f"{name} must be true or false")
    return normalized == "true"


def _optional_float(value: str, name: str) -> typing.Optional[float]:
    if value == "":
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc


def _format_number(value: typing.Optional[float]) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)


@dataclasses.dataclass
class SerialConfig(HostConfig, schemes=("serial",)):
    """Serial transport settings; credentials and profiles stay out of URIs."""

    #: Declared rather than enforced inside `_from_parsed_uri`, so the
    #: whitelist can be read before a config is built: an ambient
    #: credential (the CLI's `HOSTCTL_PASSWORD`) is offered only where it
    #: is accepted. The base dispatcher enforces it.
    #: No `username`/`password`. A console credential lives in the
    #: profile's `login=` steps and nowhere else: the two fields that used
    #: to be here were stored, whitelisted, and read by nothing, so
    #: `Host("serial:///dev/ttyUSB0", password=...)` and every
    #: `HOSTCTL_PASSWORD`-carrying CLI invocation accepted a credential
    #: that had no effect. Refusing the name is what `uri_credentials`
    #: exists for.
    uri_credentials = (
        "protocol",
        "serial_factory",
        "serial_port",
    )

    port: str
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    xonxoff: bool = False
    rtscts: bool = False
    dsrdtr: bool = False
    read_timeout: typing.Optional[float] = 0.1
    write_timeout: typing.Optional[float] = 10
    inter_byte_timeout: typing.Optional[float] = None
    exclusive: typing.Optional[bool] = None
    protocol: SerialConsoleProtocol = dataclasses.field(
        default_factory=RawConsoleProfile, repr=False, compare=False
    )
    serial_factory: typing.Optional[SerialFactory] = dataclasses.field(
        default=None, repr=False, compare=False
    )
    serial_port: typing.Optional[SerialLike] = dataclasses.field(
        default=None, repr=False, compare=False
    )
    #: The validated transport settings, assembled in `__post_init__`.
    settings: typing.Optional[SerialSettings] = dataclasses.field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        # Built ONCE, here, and kept: `SerialHost.__init__` used to build a
        # second one from the same fields as a 12-argument positional call,
        # so the two constructions could drift and the positional one had
        # no names to check against.
        settings = SerialSettings(
            self.port,
            baudrate=self.baudrate,
            bytesize=self.bytesize,
            parity=self.parity,
            stopbits=self.stopbits,
            xonxoff=self.xonxoff,
            rtscts=self.rtscts,
            dsrdtr=self.dsrdtr,
            read_timeout=self.read_timeout,
            write_timeout=self.write_timeout,
            inter_byte_timeout=self.inter_byte_timeout,
            exclusive=self.exclusive,
        )
        self.port = settings.port
        self.baudrate, self.bytesize, self.parity, self.stopbits = (
            settings.baudrate,
            settings.bytesize,
            settings.parity,
            settings.stopbits,
        )
        if not isinstance(self.protocol, SerialConsoleProtocol):
            raise TypeError("protocol must implement SerialConsoleProtocol")
        object.__setattr__(self, "settings", settings)

    @property
    def connection_uri(self) -> str:
        values: dict[str, object] = {
            "baudrate": self.baudrate,
            "bytesize": self.bytesize,
            "parity": self.parity,
            "stopbits": _format_number(self.stopbits),
            "xonxoff": str(self.xonxoff).lower(),
            "rtscts": str(self.rtscts).lower(),
            "dsrdtr": str(self.dsrdtr).lower(),
            "read_timeout": _format_number(self.read_timeout),
            "write_timeout": _format_number(self.write_timeout),
            "inter_byte_timeout": _format_number(self.inter_byte_timeout),
            "exclusive": "" if self.exclusive is None else str(self.exclusive).lower(),
        }
        return f"serial:///{quote(self.port, safe='')}?{urlencode(values)}"

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> "SerialConfig":
        port = unquote(parsed.path.lstrip("/"))
        if parsed.netloc:
            raise ValueError("serial URI must not contain an authority")
        if not port:
            raise ValueError("serial URI requires a port")
        query = strict_uri_query(
            parsed,
            (
                "baudrate",
                "bytesize",
                "parity",
                "stopbits",
                "xonxoff",
                "rtscts",
                "dsrdtr",
                "read_timeout",
                "write_timeout",
                "inter_byte_timeout",
                "exclusive",
            ),
        )

        def integer(name: str, default: int) -> int:
            try:
                return int(query.get(name, default))
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc

        return cls(
            port,
            baudrate=integer("baudrate", 115200),
            bytesize=integer("bytesize", 8),
            parity=query.get("parity", "N"),
            stopbits=float(query.get("stopbits", "1")),
            xonxoff=_bool(query.get("xonxoff", "false"), "xonxoff"),
            rtscts=_bool(query.get("rtscts", "false"), "rtscts"),
            dsrdtr=_bool(query.get("dsrdtr", "false"), "dsrdtr"),
            read_timeout=_optional_float(
                query.get("read_timeout", "0.1"), "read_timeout"
            ),
            write_timeout=_optional_float(
                query.get("write_timeout", "10"), "write_timeout"
            ),
            inter_byte_timeout=_optional_float(
                query.get("inter_byte_timeout", ""), "inter_byte_timeout"
            ),
            exclusive=(
                None
                if query.get("exclusive", "") == ""
                else _bool(query["exclusive"], "exclusive")
            ),
            protocol=typing.cast(
                SerialConsoleProtocol, credentials.get("protocol", RawConsoleProfile())
            ),
            serial_factory=typing.cast(
                typing.Optional[SerialFactory], credentials.get("serial_factory")
            ),
            serial_port=typing.cast(
                typing.Optional[SerialLike], credentials.get("serial_port")
            ),
        )

    def _create_host(self) -> "SerialHost":
        return SerialHost(self)


class SerialHost(Host):
    """Host facade over one exclusive serial console byte stream."""

    def __init__(self, config: SerialConfig) -> None:
        self.config = config
        self._executor = SerialExecutor(
            typing.cast(SerialSettings, config.settings),
            serial_factory=config.serial_factory,
            serial_port=config.serial_port,
            owns_serial_port=False if config.serial_port is not None else True,
        )
        #: The transport the negotiation was performed on, not a bare
        #: flag. `executor.close()` -- a public property's public method --
        #: dropped the port without touching a flag on this object, so the
        #: next `run()` opened a fresh port, skipped every `LoginStep`, and
        #: typed the command at the device's `login:` prompt.
        self._negotiated_on: typing.Optional[object] = None

    @property
    def executor(self) -> SerialExecutor:
        return self._executor

    @property
    def capabilities(self) -> typing.FrozenSet[str]:
        values = {"session"}
        if getattr(self.config.protocol, "can_run", False):
            values.add("run")
        return frozenset(values)

    @property
    def shell_flavour(self):
        raise NotImplementedError("serial consoles do not identify a shell flavour")

    @property
    def shell(self):
        return _SerialShell(self)

    def connect(self) -> None:
        port = self._executor.connect()
        if self._negotiated_on is port:
            return
        process = self._executor.open()
        try:
            self.config.protocol.negotiate(process)
        except TimeoutError as exc:
            # A negotiation timeout is a PROTOCOL failure, not a command
            # timeout: `negotiate()` has its own budget, and the raw
            # `TimeoutError` escaped `connect()`, `run()`, `spawn()` and
            # `with config as host:` alike -- past every
            # `except subprocess.TimeoutExpired` a caller had written, and
            # indistinguishable from a port-open failure under `OSError`.
            raise ConsoleProtocolError(
                "serial console did not complete its login exchange"
            ) from exc
        else:
            self._negotiated_on = port
        finally:
            process.close()

    def close(self) -> None:
        self._negotiated_on = None
        self._executor.close()

    def info(self) -> HostInfo:
        return HostInfo()

    def path(self, *segments: PathLike, backend: typing.Optional[str] = None):
        raise NotImplementedError("serial consoles do not provide filesystem paths")

    def spawn(
        self,
        *cmds: Command,
        executable: typing.Optional[str] = None,
        cwd: typing.Optional[PathLike] = None,
        env: typing.Optional[Environment] = None,
        terminal=None,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
    ) -> Process:
        if cmds:
            raise NotImplementedError("serial sessions do not accept startup commands")
        if executable is not None or cwd is not None or env is not None:
            raise NotImplementedError(
                "serial sessions do not support executable/cwd/env"
            )
        selected_terminal = terminal_options(terminal)
        if selected_terminal is not None and not callable(
            getattr(self.config.protocol, "terminal_setup", None)
        ):
            raise NotImplementedError("serial connections cannot allocate a PTY")
        self.connect()
        raw = self._executor.open()
        if selected_terminal is not None:
            try:
                self.config.protocol.resize(
                    raw, selected_terminal.columns, selected_terminal.rows
                )
            except Exception:
                raw.close()
                raise
        return SerialConsoleProcess(
            raw,
            self.config.protocol,
            encoding or getattr(self.config.protocol, "encoding", "utf-8"),
            errors,
            # Text only when the caller ASKED for it. The profile's encoding
            # is a write-side default; promoting it to a read mode would
            # turn every existing byte session into a text one. With either
            # keyword given, reads decode -- as they already do on the SSH
            # and QEMU console adapters, where the same call returns `str`.
            text=bool(encoding or errors),
        )

    def run(
        self,
        *cmds: Command,
        bufsize: int = -1,
        executable: typing.Optional[str] = None,
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
    ) -> subprocess.CompletedProcess:
        if not self.config.protocol.can_run:
            raise NotImplementedError(
                "serial console profile does not provide reliable run()"
            )
        if starts_direct_command(cmds) is not None:
            raise NotImplementedError(
                "serial consoles do not provide native argv execution"
            )
        if executable is not None:
            raise NotImplementedError("serial consoles do not accept executable")
        if cwd is not None or env is not None:
            raise NotImplementedError("serial console profile does not support cwd/env")
        if input is not None:
            raise NotImplementedError("serial framed run does not support stdin input")
        if stdin is not None:
            # Accepted and dropped, this looked like a second way to feed a
            # command -- while `input=` right above refuses explicitly.
            raise NotImplementedError(
                "serial framed run does not support stdin redirection"
            )
        if bufsize != -1:
            raise NotImplementedError(
                "serial framed run has no pipe to size: bufsize is not supported"
            )
        rendered = []
        for value in cmds:
            if isinstance(value, (tuple, list)):
                # `shlex.join` is POSIX quoting, and this host refuses to
                # name a shell flavour at all. On a Cisco-style console
                # `["show", "run | include hostname"]` went on the wire as
                # `show 'run | include hostname'`, which the device takes
                # literally and rejects. The profile owns the device's
                # grammar; nothing here knows it.
                raise NotImplementedError(
                    "a serial console profile defines no argv quoting: pass "
                    "the command as text, spelled the way the device expects"
                )
            rendered.append(str(value))
        self.connect()
        process = self._executor.open()
        deadline = None if timeout is None else time.monotonic() + timeout
        outputs = []
        returncode = 0
        command = rendered[0] if len(rendered) == 1 else list(rendered)
        try:
            for index, single in enumerate(rendered):
                remaining = (
                    None if deadline is None else max(0.0, deadline - time.monotonic())
                )
                try:
                    # One framed exchange per command. Joined with `";"` they
                    # went out as one line, which most device consoles parse
                    # as a single malformed command rather than two -- and a
                    # profile that frames status per exchange could only
                    # report one status for all of them.
                    output, returncode = self.config.protocol.run(
                        process, single, timeout=remaining
                    )
                except TimeoutError as exc:
                    # `orphaned=True`: a serial console has no way to stop
                    # what the device is doing, so the command is still
                    # running there. One payload shape for every transport
                    # (see `executor.expired`).
                    partial = b"".join(outputs) + (getattr(exc, "output", None) or b"")
                    raise expired(
                        command,
                        timeout,
                        output=partial,
                        orphaned=True,
                        text=bool(text or encoding or errors),
                    ) from exc
                outputs.append(output)
                del index
        finally:
            process.close()
        output = b"".join(outputs)
        output_stream, _error_stream = capture_streams(capture_output, stdout, stderr)
        if wants_text(text, encoding, errors):
            output_value: typing.Union[str, bytes] = output.decode(
                encoding or "utf-8", errors or "strict"
            )
        else:
            output_value = output
        # The shared dispatcher owns the "a `None` target means `sys.stdout`"
        # rule; serial used to treat `None` as "discard", which silently threw
        # the console transcript away for `capture_output=False`.  A serial
        # console is one merged stream, so the stderr side is pinned to PIPE
        # to keep the dispatcher from writing an empty second stream.
        captured, _ = dispatch_output(
            output_stream,
            subprocess.PIPE,
            output_value,
            None,
            encoding=encoding,
            errors=errors,
        )
        result = subprocess.CompletedProcess(command, returncode, captured, None)
        if check and returncode:
            raise subprocess.CalledProcessError(
                returncode, command, output=captured, stderr=None
            )
        return result


class _SerialShell:
    """Minimal shell binding for consoles with no declared OS shell.

    Not a `Shell`: there is no flavour to bind, so quoting, `cwd`, `env` and
    the encoding defaults a `Shell` carries have no meaning here. What it
    does supply is the two spellings a caller reaches for -- `run()` (only
    when the profile frames a reliable status) and `session()` -- plus the
    documented `with host.shell as session:` shorthand, which is a serial
    console's primary mode and used to raise `TypeError: object does not
    support the context manager protocol`.
    """

    def __init__(self, host: SerialHost) -> None:
        self.host = host
        self._session = None

    def run(self, *cmds, **options):
        return self.host.run(*cmds, **options)

    def session(self, *cmds, **options):
        process = self.host.spawn(**options)
        if cmds:
            for command in cmds:
                process.send_command(command)
        return process

    def __enter__(self):
        if self._session is not None:
            raise RuntimeError("this shell already has an open session")
        self._session = self.session()
        return self._session

    def __exit__(self, exc_type, exc_value, traceback):
        session, self._session = self._session, None
        if session is not None:
            session.close()
        return False

    def __call__(self, *args, **options):
        raise NotImplementedError(
            "a serial console has no shell flavour, so cwd/env/encoding "
            "defaults cannot be bound; use host.spawn(...) or "
            "host.shell.session(...)"
        )

    def configure(self, **options):
        return self(**options)

    def execute(self, command, *args, **options):
        raise NotImplementedError(
            "a serial console has no shell flavour to quote an argv with; "
            "send the command text through run() or a session"
        )


__all__ = ["SerialConfig", "SerialHost"]
