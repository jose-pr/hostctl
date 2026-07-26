"""Serial-console host and opaque serial connection configuration."""

from __future__ import annotations

import dataclasses
import shlex
import subprocess
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
)
from ..executor._common import write_output
from ..process import Process, SerialConsoleProcess, terminal_options
from ..serial import PromptConsoleProfile, RawConsoleProfile, SerialConsoleProtocol
from ._common import (
    Command,
    Host,
    HostConfig,
    HostInfo,
    PathLike,
    starts_direct_command,
    strict_uri_credentials,
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
    username: typing.Optional[str] = dataclasses.field(default=None, repr=False)
    password: typing.Optional[str] = dataclasses.field(default=None, repr=False)
    serial_factory: typing.Optional[SerialFactory] = dataclasses.field(
        default=None, repr=False, compare=False
    )
    serial_port: typing.Optional[SerialLike] = dataclasses.field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
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
        strict_uri_credentials(
            credentials,
            ("protocol", "username", "password", "serial_factory", "serial_port"),
        )
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
            username=typing.cast(typing.Optional[str], credentials.get("username")),
            password=typing.cast(typing.Optional[str], credentials.get("password")),
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
        settings = SerialSettings(
            config.port,
            config.baudrate,
            config.bytesize,
            config.parity,
            config.stopbits,
            config.xonxoff,
            config.rtscts,
            config.dsrdtr,
            config.read_timeout,
            config.write_timeout,
            config.inter_byte_timeout,
            config.exclusive,
        )
        self._executor = SerialExecutor(
            settings,
            serial_factory=config.serial_factory,
            serial_port=config.serial_port,
            owns_serial_port=False if config.serial_port is not None else True,
        )
        self._negotiated = False

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
        if self._negotiated:
            return
        self._executor.connect()
        process = self._executor.open()
        try:
            self.config.protocol.negotiate(process)
            self._negotiated = True
        finally:
            process.close()

    def close(self) -> None:
        self._negotiated = False
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
        command = ";".join(
            (
                shlex.join([str(item) for item in value])
                if isinstance(value, (tuple, list))
                else str(value)
            )
            for value in cmds
        )
        self.connect()
        process = self._executor.open()
        try:
            output, returncode = self.config.protocol.run(
                process, command, timeout=timeout
            )
        except TimeoutError as exc:
            raise subprocess.TimeoutExpired(
                command, timeout, output=getattr(exc, "output", None)
            ) from exc
        finally:
            process.close()
        output_stream, _error_stream = capture_streams(capture_output, stdout, stderr)
        use_text = bool(text) if text is not None else encoding is not None
        if use_text:
            output_value: typing.Union[str, bytes] = output.decode(
                encoding or "utf-8", errors or "strict"
            )
        else:
            output_value = output
        captured = output_value if output_stream == subprocess.PIPE else None
        if output_stream not in (None, subprocess.PIPE, subprocess.DEVNULL):
            write_output(
                output_stream,
                output_value,
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
    """Minimal shell binding for consoles with no declared OS shell."""

    def __init__(self, host: SerialHost) -> None:
        self.host = host

    def run(self, *cmds, **options):
        return self.host.run(*cmds, **options)

    def session(self, *cmds, **options):
        process = self.host.spawn(**options)
        if cmds:
            for command in cmds:
                process.send_command(command)
        return process


__all__ = ["SerialConfig", "SerialHost"]
