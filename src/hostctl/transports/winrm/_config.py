"""WinRM configuration: endpoint, authentication, TLS and leg selection."""

from __future__ import annotations


import dataclasses
import typing
from urllib.parse import quote, unquote, urlencode


from ...uri._parse import (
    _query_bool,
    _refuse_uri,
    _query_int,
    strict_uri_query,
    uri_host,
    uri_hostname,
)
from ...host._config import HostConfig

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

WinRMProviderName = typing.Literal["auto", "native", "pywinrm", "psrp"]


@dataclasses.dataclass
class WinRMConfig(HostConfig, schemes=("winrm", "winrms")):
    """Connection and transport settings for the Windows provider."""

    #: Declared rather than enforced inside `_from_parsed_uri`, so the
    #: whitelist can be read before a config is built: an ambient
    #: credential (the CLI's `HOSTCTL_PASSWORD`) is offered only where it
    #: is accepted. The base dispatcher enforces it.
    uri_credentials = ("password",)

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
    provider: WinRMProviderName = "auto"
    #: Opt in to Basic credentials over plain HTTP; see `__post_init__`.
    allow_cleartext: bool = False

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.host:
            raise ValueError("host must not be empty")
        _refuse_uri("WinRMConfig", self.host)
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
        if self.provider not in typing.get_args(WinRMProviderName):
            raise ValueError("provider must be 'auto', 'native', 'pywinrm', or 'psrp'")
        if self.provider == "native" and self.password is not None:
            raise ValueError(
                "native WinRM uses the current Windows logon; drop password= "
                "or choose 'pywinrm'/'psrp'"
            )
        if (
            self.provider in ("pywinrm", "psrp")
            and self.password is None
            and self.transport != "kerberos"
        ):
            raise ValueError(
                f"provider={self.provider!r} needs password= (or "
                "transport='kerberos'); use provider='native' for the "
                "current Windows logon"
            )
        if (
            self.transport in ("basic", "plaintext", "ssl")
            and not self.ssl
            and not self.allow_cleartext
        ):
            raise ValueError(
                f"transport={self.transport!r} over http sends the password in "
                "cleartext; use ssl=True (winrms://) or allow_cleartext=True"
            )

    @property
    def endpoint(self) -> str:
        scheme = "https" if self.ssl else "http"
        from netimps import get_default_port, join_host

        port = self.port or get_default_port(self.scheme)
        return f"{scheme}://{join_host(self.host, port)}/wsman"

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
        if self.provider != "auto":
            query += "&" + urlencode({"provider": self.provider})
        if self.allow_cleartext:
            query += "&allow_cleartext=true"
        authority = uri_host(self.host)
        if self.port is not None:
            from netimps import join_host

            authority = join_host(self.host, self.port)
        return f"{self.scheme}://{quote(self.username, safe='')}@{authority}?{query}"

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> WinRMConfig:
        query = strict_uri_query(
            parsed,
            {
                "transport",
                "server_cert_validation",
                "message_encryption",
                "operation_timeout_sec",
                "read_timeout_sec",
                "provider",
                "allow_cleartext",
            },
        )
        if not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("WinRM URI requires a host and no path")
        if not parsed.username:
            raise ValueError("WinRM URI requires a username")
        return cls(
            host=uri_hostname(parsed),
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
            provider=typing.cast(WinRMProviderName, query.get("provider", "auto")),
            allow_cleartext=_query_bool(query, "allow_cleartext", False),
        )

    def _create_host(self):
        from ._providers import WinRMExecutorProvider
        from ._providers import WinRMPathProvider
        from ._transport import _WinRMTransport

        from ...system._kinds import WindowsHost

        transport = _WinRMTransport(self)
        return WindowsHost(
            self,
            executor_providers=(WinRMExecutorProvider(transport),),
            path_providers=(WinRMPathProvider(transport),),
        )
