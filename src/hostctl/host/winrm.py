"""Windows Remote Management host implementation."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import typing
from urllib.parse import quote, unquote, urlencode

from ..executor import NativeWinRMSession, WinRMExecutor, WinRMSession
from ._common import (
    _query_int,
    CaptureOutput,
    Command,
    Environment,
    FileHandle,
    Host,
    HostConfig,
    HostInfo,
    Input,
    PathLike,
    parse_host_info,
    is_direct_command,
    strict_uri_credentials,
    strict_uri_query,
    uri_host,
)
from ..shell import POWERSHELL, ShellFlavour
from .winrm_path import WinRMPath, WinRMPathBackend

WinRMTransport = typing.Literal[
    "basic",
    "credssp",
    "kerberos",
    "ntlm",
    "plaintext",
    "ssl",
]
CertificateValidation = typing.Literal["validate", "ignore"]
MessageEncryption = typing.Literal["auto", "always", "never"]


@dataclasses.dataclass
class WinRMConfig(HostConfig, schemes=("winrm", "winrms")):
    """Connection and transport settings accepted by :class:`WinRMHost`."""

    host: str
    username: str
    password: typing.Optional[str] = dataclasses.field(default=None, repr=False)
    transport: WinRMTransport = "ntlm"
    port: typing.Optional[int] = None
    ssl: bool = False
    server_cert_validation: CertificateValidation = "validate"
    message_encryption: MessageEncryption = "auto"
    operation_timeout_sec: int = 20
    read_timeout_sec: int = 30

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.host:
            raise ValueError("host must not be empty")
        if not self.username:
            raise ValueError("username must not be empty")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.transport not in typing.get_args(WinRMTransport):
            raise ValueError(f"unsupported WinRM transport: {self.transport}")
        if self.server_cert_validation not in typing.get_args(CertificateValidation):
            raise ValueError("server_cert_validation must be 'validate' or 'ignore'")
        if self.message_encryption not in typing.get_args(MessageEncryption):
            raise ValueError("message_encryption must be 'auto', 'always', or 'never'")
        if self.operation_timeout_sec <= 0:
            raise ValueError("operation_timeout_sec must be positive")
        if self.read_timeout_sec <= self.operation_timeout_sec:
            raise ValueError(
                "read_timeout_sec must be greater than operation_timeout_sec"
            )

    @property
    def endpoint(self) -> str:
        scheme = "https" if self.ssl else "http"
        port = self.port or (5986 if self.ssl else 5985)
        return f"{scheme}://{uri_host(self.host)}:{port}/wsman"

    @property
    def scheme(self) -> str:
        return "winrms" if self.ssl else "winrm"

    @property
    def connection_uri(self) -> str:
        query = urlencode(
            {
                "transport": self.transport,
                "server_cert_validation": self.server_cert_validation,
                "message_encryption": self.message_encryption,
                "operation_timeout_sec": self.operation_timeout_sec,
                "read_timeout_sec": self.read_timeout_sec,
            }
        )
        port = self.port or (5986 if self.ssl else 5985)
        return (
            f"{self.scheme}://{quote(self.username, safe='')}@"
            f"{uri_host(self.host)}:{port}?{query}"
        )

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> WinRMConfig:
        strict_uri_credentials(credentials, ("password",))
        query = strict_uri_query(
            parsed,
            {
                "transport",
                "server_cert_validation",
                "message_encryption",
                "operation_timeout_sec",
                "read_timeout_sec",
            },
        )
        if not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("WinRM URI requires a host and no path")
        if not parsed.username:
            raise ValueError("WinRM URI requires a username")
        return cls(
            host=parsed.hostname,
            username=unquote(parsed.username),
            password=typing.cast(typing.Optional[str], credentials.get("password")),
            port=parsed.port,
            ssl=parsed.scheme.casefold() == "winrms",
            transport=typing.cast(WinRMTransport, query.get("transport", "ntlm")),
            server_cert_validation=typing.cast(
                CertificateValidation,
                query.get("server_cert_validation", "validate"),
            ),
            message_encryption=typing.cast(
                MessageEncryption,
                query.get("message_encryption", "auto"),
            ),
            operation_timeout_sec=_query_int(query, "operation_timeout_sec", 20),
            read_timeout_sec=_query_int(query, "read_timeout_sec", 30),
        )

    def _create_host(self) -> WinRMHost:
        return WinRMHost(self)


class WinRMHost(Host):
    """A Windows host reached through PowerShell over WinRM."""

    def __init__(self, config: WinRMConfig) -> None:
        self.config = config
        self._session: typing.Optional[WinRMSession] = None
        self._executor = WinRMExecutor(
            lambda: self.session,
            lambda: float(self.config.read_timeout_sec),
        )
        self._path_backend = WinRMPathBackend(self.run)

    @property
    def capabilities(self) -> typing.FrozenSet[str]:
        return frozenset(("run", "path"))

    @property
    def shell_flavour(self) -> ShellFlavour:
        return POWERSHELL

    @property
    def executor(self) -> WinRMExecutor:
        return self._executor

    def info(self) -> HostInfo:
        return parse_host_info(
            self.run(
                POWERSHELL.info_script,
                check=False,
                encoding="utf-8",
            ).stdout
        )

    @property
    def session(self) -> WinRMSession:
        if self._session is None:
            if self.config.password is None and os.name == "nt":
                current = (
                    os.environ.get("USERDOMAIN", "")
                    + "\\"
                    + os.environ.get("USERNAME", "")
                ).strip("\\")
                if current and self.config.username.casefold() != current.casefold():
                    raise ValueError(
                        "password-free native WinRM requires the current Windows user"
                    )
                self._session = NativeWinRMSession(
                    self.config.host,
                    ssl=self.config.ssl,
                    port=self.config.port,
                    timeout=float(self.config.read_timeout_sec),
                )
                return self._session
            try:
                import winrm
            except ImportError as exc:
                raise ImportError(
                    "WinRM support requires the 'winrm' extra: "
                    "pip install hostctl[winrm]"
                ) from exc
            self._session = winrm.Session(
                self.config.endpoint,
                auth=(self.config.username, self.config.password),
                transport=self.config.transport,
                server_cert_validation=self.config.server_cert_validation,
                message_encryption=self.config.message_encryption,
                operation_timeout_sec=self.config.operation_timeout_sec,
                read_timeout_sec=self.config.read_timeout_sec,
            )
        return self._session

    def connect(self) -> None:
        _ = self.session

    def close(self) -> None:
        if self._session is None:
            return
        session, self._session = self._session, None
        close = getattr(session, "close", None)
        if close is not None:
            close()

    def path(
        self, *segments: PathLike, backend: typing.Optional[str] = None
    ) -> WinRMPath:
        if backend not in (None, "winrm"):
            raise ValueError("WinRMHost path backend must be 'winrm'")
        if (
            len(segments) > 1
            and isinstance(segments[0], str)
            and len(segments[0]) == 2
            and segments[0].endswith(":")
        ):
            segments = (segments[0] + "\\", *segments[1:])
        return WinRMPath(*segments, backend=self._path_backend)

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
        if executable is not None:
            raise NotImplementedError(
                "WinRMHost.run supports PowerShell only and does not accept executable"
            )
        if is_direct_command(cmds):
            command = cmds[0]
            cmds = (command if isinstance(command, (tuple, list)) else (command,),)
        script = POWERSHELL.script(cmds, cwd=cwd, env=env)
        return self.executor(
            script,
            bufsize=bufsize,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            capture_output=capture_output,
            check=check,
            encoding=encoding,
            errors=errors,
            input=input,
            timeout=timeout,
            text=text,
        )
