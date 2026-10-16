"""WinRM configuration: endpoint, authentication, TLS and leg selection."""

from __future__ import annotations


import dataclasses
import typing
from urllib.parse import quote, unquote, urlencode


from ...uri._parse import (
    _query_bool,
    _refuse_uri,
    _query_float,
    _seconds_text,
    strict_uri_query,
    uri_host,
    uri_hostname,
)
from ...host._config import HostConfig
from ...host._dispatch import _keyword_only

WinRMAuth = typing.Literal[
    "basic",
    "credssp",
    "kerberos",
    "ntlm",
    "plaintext",
    "ssl",
]

MessageEncryption = typing.Literal["auto", "always", "never"]

WinRMProviderName = typing.Literal["auto", "native", "pywinrm", "psrp"]


@_keyword_only()
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
    auth: WinRMAuth = "ntlm"
    port: typing.Optional[int] = None
    tls: bool = False
    verify: bool = True
    message_encryption: MessageEncryption = "auto"
    operation_timeout: float = 20.0
    read_timeout: float = 30.0
    provider: WinRMProviderName = "auto"
    #: Opt in to Basic credentials over plain HTTP; see `__post_init__`.
    cleartext_auth: bool = False

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.host:
            raise ValueError("host must not be empty")
        _refuse_uri("WinRMConfig", self.host)
        if not self.username:
            raise ValueError("username must not be empty")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.auth not in typing.get_args(WinRMAuth):
            raise ValueError(f"unsupported WinRM auth: {self.auth}")
        if not isinstance(self.verify, bool):
            raise TypeError("verify must be True or False")
        if self.message_encryption not in typing.get_args(MessageEncryption):
            raise ValueError("message_encryption must be 'auto', 'always', or 'never'")
        if self.operation_timeout <= 0:
            raise ValueError("operation_timeout must be positive")
        if self.read_timeout <= self.operation_timeout:
            raise ValueError("read_timeout must be greater than operation_timeout")
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
            and self.auth != "kerberos"
        ):
            raise ValueError(
                f"provider={self.provider!r} needs password= (or "
                "auth='kerberos'); use provider='native' for the "
                "current Windows logon"
            )
        if (
            self.auth in ("basic", "plaintext", "ssl")
            and not self.tls
            and not self.cleartext_auth
        ):
            raise ValueError(
                f"auth={self.auth!r} over http sends the password in "
                "cleartext; use tls=True (winrms://) or cleartext_auth=True"
            )

    @property
    def endpoint(self) -> str:
        scheme = "https" if self.tls else "http"
        from netimps import get_default_port, join_host

        port = self.port or get_default_port(self.scheme)
        return f"{scheme}://{join_host(self.host, port)}/wsman"

    @property
    def scheme(self) -> str:
        return "winrms" if self.tls else "winrm"

    @property
    def connection_uri(self) -> str:
        query = urlencode(
            {
                "auth": self.auth,
                "verify": str(self.verify).lower(),
                "message_encryption": self.message_encryption,
                "operation_timeout": _seconds_text(self.operation_timeout),
                "read_timeout": _seconds_text(self.read_timeout),
            }
        )
        if self.provider != "auto":
            query += "&" + urlencode({"provider": self.provider})
        if self.cleartext_auth:
            query += "&cleartext_auth=true"
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
                "auth",
                "verify",
                "message_encryption",
                "operation_timeout",
                "read_timeout",
                "provider",
                "cleartext_auth",
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
            tls=parsed.scheme.casefold() == "winrms",
            auth=typing.cast(WinRMAuth, query.get("auth", "ntlm")),
            verify=_query_bool(query, "verify", True),
            message_encryption=typing.cast(
                MessageEncryption,
                query.get("message_encryption", "auto"),
            ),
            operation_timeout=_query_float(query, "operation_timeout", 20.0),
            read_timeout=_query_float(query, "read_timeout", 30.0),
            provider=typing.cast(WinRMProviderName, query.get("provider", "auto")),
            cleartext_auth=_query_bool(query, "cleartext_auth", False),
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
