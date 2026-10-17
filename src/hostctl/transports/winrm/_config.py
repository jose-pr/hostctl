"""WinRM configuration: endpoint, authentication, TLS and leg selection."""

from __future__ import annotations


import dataclasses
import os
import typing
from urllib.parse import quote, unquote, urlencode


from ...uri._parse import (
    _query_bool,
    _refuse_uri,
    strict_uri_query,
    uri_host,
    uri_hostname,
)
from ...host._config import HostConfig
from ...host._dispatch import _keyword_only
from ...host._verify import (
    _TLSVerify,
    _tls_verify,
    _verify_from_query,
    _verify_query_text,
)
from ...host._timeout import (
    _URI_TIMEOUT_KEYS,
    _TimeoutField,
    _refuse_renamed_keys,
    _timeout_from_query,
    _timeout_query_text,
)

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

#: The WS-Man `OperationTimeout` in seconds: how long the server may hold a
#: Receive open while the command prints nothing.
_OPERATION_TIMEOUT = 20
#: The HTTP read limit of one request. It must outlast the server's hold, or
#: every quiet Receive would end as a client-side read timeout.
_READ_TIMEOUT = _OPERATION_TIMEOUT + 10

#: Per-request knobs a URI may still carry; request timing is not configurable.
_REMOVED_QUERY_KEYS = (
    "operation_timeout",
    "read_timeout",
    "operation_timeout_sec",
    "read_timeout_sec",
)

#: Why the native leg refuses a CA bundle: PowerShell remoting's session
#: options can skip certificate checks but cannot name a trust source.
_NATIVE_BUNDLE = (
    "WinRMConfig: native WinRM trusts the Windows certificate store and takes "
    "no CA bundle; import the CA there, or use provider='pywinrm' or 'psrp' "
    "(with password= or auth='kerberos')"
)


@_keyword_only()
@dataclasses.dataclass
class WinRMConfig(HostConfig, schemes=("winrm", "winrms")):
    """Connection and transport settings for the Windows provider."""

    #: Declared rather than enforced inside `_from_parsed_uri`, so the
    #: whitelist can be read before a config is built: an ambient
    #: credential (the CLI's `HOSTCTL_PASSWORD`) is offered only where it
    #: is accepted. The base dispatcher enforces it.
    uri_credentials = ("password",)

    _timeout_defaults = {"total": 30.0, "connect": 30.0}

    host: str
    username: str
    password: typing.Optional[str] = dataclasses.field(default=None, repr=False)
    auth: WinRMAuth = "ntlm"
    port: typing.Optional[int] = None
    tls: bool = False
    #: `True` checks the server certificate against the operating system's
    #: trust store (pywinrm and PSRP: `REQUESTS_CA_BUNDLE` or `CURL_CA_BUNDLE`
    #: instead when set), `False` checks nothing, a path is the CA bundle
    #: trusted instead (pywinrm and PSRP only). Stored as `bool` or `str`.
    verify: typing.Union[bool, str, "os.PathLike[str]"] = True
    message_encryption: MessageEncryption = "auto"
    #: `total` is the default of `run(timeout=)` on every leg (30 s);
    #: `connect` bounds establishing the connection or session (30 s), and
    #: `None` there leaves each leg's library default.
    timeout: _TimeoutField = _TimeoutField()
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
        self.verify = _tls_verify("WinRMConfig", self.verify)
        if isinstance(self.verify, str):
            if not self.tls:
                raise ValueError(
                    "WinRMConfig: a CA bundle (verify=path) needs tls=True "
                    "(winrms://); plain HTTP has no certificate to check"
                )
            if self.provider == "native":
                raise ValueError(_NATIVE_BUNDLE)
        if self.message_encryption not in typing.get_args(MessageEncryption):
            raise ValueError("message_encryption must be 'auto', 'always', or 'never'")
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
                "verify": _verify_query_text(typing.cast(_TLSVerify, self.verify)),
                "message_encryption": self.message_encryption,
            }
        )
        query += _timeout_query_text(self.timeout)
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
                "connect_timeout",
                "provider",
                "cleartext_auth",
                *_URI_TIMEOUT_KEYS,
                *_REMOVED_QUERY_KEYS,
            },
        )
        _refuse_renamed_keys(query, {"connect_timeout": "timeout_connect"})
        for key in _REMOVED_QUERY_KEYS:
            if key in query:
                raise ValueError(
                    f"unknown connection parameter: {key}; WinRM takes "
                    "timeout_connect (establishing the connection) and "
                    "timeout (the whole command)"
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
            verify=_verify_from_query(query, bundle=True),
            message_encryption=typing.cast(
                MessageEncryption,
                query.get("message_encryption", "auto"),
            ),
            timeout=_timeout_from_query(query),
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
