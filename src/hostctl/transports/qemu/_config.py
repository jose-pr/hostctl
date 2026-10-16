"""QEMU guest configuration: domain, guest-agent transport and shell."""

from __future__ import annotations


import dataclasses
import typing
from urllib.parse import quote, unquote, urlencode


from ._qga import GuestAgentTransport
from ._console import QEMUSerialConsole
from ...shell import shell_flavour
from ...host._config import HostConfig
from ...host._dispatch import _keyword_only
from ...host._selection import PathnameSelection, ShellSelection
from ...host._timeout import (
    DEFAULT,
    Timeout,
    _URI_TIMEOUT_KEYS,
    _TimeoutField,
    _refuse_renamed_keys,
    _timeout_from_query,
    _timeout_query,
)
from ...host._verify import _verify_from_query
from ...uri._parse import (
    pathname_label,
    pathname_selection,
    strict_uri_query,
    uri_hostname,
)
from ..ssh._config import SSHConfig
from ._helper import GuestPathHelper

if typing.TYPE_CHECKING:
    from ._host import QEMUHost


QEMUTransport = typing.Literal["libvirt", "unix", "ssh"]


@_keyword_only()
@dataclasses.dataclass
class QEMUConfig(HostConfig, schemes=("qemu+libvirt", "qga+unix", "qga+ssh")):
    """Guest identity, QGA transport, and guest semantic selections."""

    #: Declared rather than enforced inside `_from_parsed_uri`, so the
    #: whitelist can be read before a config is built: an ambient
    #: credential (the CLI's `HOSTCTL_PASSWORD`) is offered only where it
    #: is accepted. The base dispatcher enforces it.
    uri_credentials = (
        "password",
        "client_keys",
        "known_hosts",
        "transport_factory",
        "serial_console",
        "path_helper",
    )

    #: `connect` resolves to the reply limit, and only where hostctl opens
    #: the agent stream itself; see `timeouts`.
    _timeout_defaults = {"total": None, "connect": DEFAULT, "read": 10.0}

    domain: str
    transport: QEMUTransport = "libvirt"
    connection: typing.Optional[str] = None
    socket_path: typing.Optional[str] = None
    ssh: typing.Optional[SSHConfig] = dataclasses.field(default=None, repr=False)
    #: `total` is the default of `run(timeout=)` (none); `read` bounds one
    #: guest-agent reply (10 s); `connect` opening the agent stream and its
    #: guest-sync on the unix and ssh transports (the `read` value).
    timeout: _TimeoutField = _TimeoutField()
    #: Largest QGA reply this host will accept, in bytes. qemu-ga caps its
    #: own captured output at 16 MiB per stream and sends it Base64-encoded
    #: inside JSON, so a command producing that much needed roughly 21 MiB
    #: of reply to survive -- against a hard-wired 8 MiB the transports
    #: could not be told to raise. A rejected reply is not recoverable: the
    #: output is already gone.
    max_reply_size: int = 48 * 1024 * 1024
    shell: ShellSelection = "auto"
    pathname: PathnameSelection = "auto"
    transport_factory: typing.Optional[typing.Callable[[], GuestAgentTransport]] = (
        dataclasses.field(default=None, repr=False, compare=False)
    )
    serial_console: typing.Optional[QEMUSerialConsole] = dataclasses.field(
        default=None, repr=False, compare=False
    )
    #: Guest-side helper supplying the metadata and namespace operations QGA
    #: has no RPC for. QGA's file RPCs move bytes and nothing else, so without
    #: one `stat`, `scandir` and every mutation are unavailable and the path
    #: provider reports `degraded`. hostctl ships no implementation yet;
    #: supplying one is how a caller with a known guest gets a complete path
    #: surface today.
    path_helper: typing.Optional["GuestPathHelper"] = dataclasses.field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.domain or "\x00" in self.domain:
            raise ValueError("domain must not be empty or contain NUL")
        if self.transport not in typing.get_args(QEMUTransport):
            raise ValueError(f"unsupported QEMU transport: {self.transport}")
        if self.timeout.connect is not DEFAULT and not self._opens_agent_stream:
            raise ValueError(
                "QEMUConfig: timeout.connect is not supported with "
                + (
                    "transport_factory"
                    if self.transport_factory is not None
                    else "transport='libvirt'"
                )
            )
        if self.max_reply_size <= 0:
            raise ValueError("max_reply_size must be positive")
        if self.transport == "unix" and not self.socket_path:
            raise ValueError("unix QGA transport requires socket_path")
        if self.transport == "ssh" and self.ssh is None:
            raise ValueError("SSH QGA transport requires an SSHConfig")
        if self.transport != "ssh" and self.ssh is not None:
            raise ValueError("ssh config requires transport='ssh'")
        if self.shell != "auto":
            self.shell = shell_flavour(self.shell)
        self.pathname = pathname_selection(self.pathname, allow_auto=True)

    @property
    def _opens_agent_stream(self) -> bool:
        """Whether hostctl connects the agent stream (unix, ssh) itself."""
        return self.transport != "libvirt" and self.transport_factory is None

    @property
    def timeouts(self) -> Timeout:
        timeouts = super().timeouts
        if timeouts.connect is DEFAULT and self._opens_agent_stream:
            return dataclasses.replace(timeouts, connect=timeouts.read)
        return timeouts

    def _default_name(self) -> str:
        return self.domain

    @property
    def connection_uri(self) -> str:
        shell = "auto" if self.shell == "auto" else str(self.shell)
        pathname = pathname_label(self.pathname)
        query: typing.Dict[str, object] = {
            "max_reply_size": self.max_reply_size,
            "shell": shell,
            "pathname": pathname,
        }
        query.update(_timeout_query(self.timeout))
        if self.transport == "libvirt":
            if self.connection is not None:
                query["connection"] = self.connection
            return (
                f"qemu+libvirt:///{quote(self.domain, safe='')}?" f"{urlencode(query)}"
            )
        if self.transport == "unix":
            query["domain"] = self.domain
            return (
                f"qga+unix:///{quote(self.socket_path or '', safe='')}?"
                f"{urlencode(query)}"
            )
        ssh = typing.cast(SSHConfig, self.ssh)
        query["socket_path"] = self.socket_path or (
            f"/run/qemu-server/{self.domain}.qga"
        )
        from netimps import join_host

        if not ssh.verify:
            query["verify"] = "false"
        user = f"{quote(ssh.username, safe='')}@" if ssh.username else ""
        authority = f"{user}{join_host(ssh.host, ssh.port)}"
        return (
            f"qga+ssh://{authority}/{quote(self.domain, safe='')}?"
            f"{urlencode(query)}"
        )

    #: Credentials every QEMU scheme accepts. `password`, `client_keys` and
    #: `known_hosts` are meaningful only where an SSH tunnel carries the
    #: agent stream, so only `qga+ssh` takes them.
    _COMMON_CREDENTIALS = ("transport_factory", "serial_console", "path_helper")

    @classmethod
    def _uri_credentials_for(cls, parsed):
        # A `password=` on a libvirt or Unix socket URI would read as
        # authentication that had been applied.
        if parsed.scheme.casefold() != "qga+ssh":
            return cls._COMMON_CREDENTIALS
        return cls.uri_credentials

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> QEMUConfig:
        scheme = parsed.scheme.casefold()
        query = strict_uri_query(
            parsed,
            (
                "agent_timeout",
                "connection",
                "shell",
                "domain",
                "max_reply_size",
                "pathname",
                "socket_path",
                "verify",
                *_URI_TIMEOUT_KEYS,
            ),
        )
        if "verify" in query and scheme != "qga+ssh":
            # libvirt and a local socket have no peer for hostctl to verify.
            raise ValueError("verify applies only to a qga+ssh URI")
        _refuse_renamed_keys(query, {"agent_timeout": "timeout_read"})
        try:
            max_reply_size = int(query.get("max_reply_size", cls.max_reply_size))
        except ValueError as exc:
            raise ValueError("max_reply_size must be an integer") from exc
        common = {
            "timeout": _timeout_from_query(query),
            "max_reply_size": max_reply_size,
            "shell": (
                "auto"
                if query.get("shell", "auto") == "auto"
                else shell_flavour(query["shell"])
            ),
            "pathname": pathname_selection(
                query.get("pathname", "auto"), allow_auto=True
            ),
            "transport_factory": credentials.get("transport_factory"),
            "serial_console": credentials.get("serial_console"),
            "path_helper": credentials.get("path_helper"),
        }
        if scheme == "qemu+libvirt":
            if parsed.netloc or not parsed.path.strip("/"):
                raise ValueError("libvirt QEMU URI requires a domain path")
            return cls(
                unquote(parsed.path.lstrip("/")),
                transport="libvirt",
                connection=query.get("connection") or None,
                **common,
            )
        if scheme == "qga+unix":
            path = unquote(parsed.path.lstrip("/"))
            domain = query.get("domain")
            if not path or not domain:
                raise ValueError("Unix QGA URI requires socket path and domain")
            if not path.startswith("/"):
                path = "/" + path
            return cls(
                domain,
                transport="unix",
                socket_path=path,
                **common,
            )
        if not parsed.hostname or not parsed.username or not parsed.path.strip("/"):
            raise ValueError("SSH QGA URI requires user, host, and domain")
        from netimps import get_default_port

        ssh = SSHConfig(
            uri_hostname(parsed),
            port=parsed.port or get_default_port("ssh"),
            username=unquote(parsed.username),
            password=typing.cast(typing.Optional[str], credentials.get("password")),
            client_keys=credentials.get("client_keys"),
            verify=typing.cast(bool, _verify_from_query(query, bundle=False)),
            known_hosts=credentials.get("known_hosts", ()),
        )
        return cls(
            unquote(parsed.path.lstrip("/")),
            transport="ssh",
            socket_path=query.get("socket_path") or None,
            ssh=ssh,
            **common,
        )

    def _create_host(self) -> QEMUHost:
        from ._host import QEMUHost

        return QEMUHost(self)
