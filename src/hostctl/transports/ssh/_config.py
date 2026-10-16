"""SSH configuration: target, credentials, policy and shell selection."""

from __future__ import annotations

import dataclasses


import os


import typing

from pathlib import PurePath, PureWindowsPath

from urllib.parse import quote, unquote, urlencode

from pathapi import Pathname, PosixPathname


from ...executor import PathLike

from ...host._config import HostConfig


from ...uri._parse import (
    _refuse_uri,
    path_flavor_name,
    path_flavor_selection,
    strict_uri_query,
    uri_hostname,
)

from ...shell import POSIX_SHELL, ShellFlavourSelection, shell_flavour

if typing.TYPE_CHECKING:
    from ._transport import _SSHTransport


SSHKeySource = typing.Union[str, bytes, os.PathLike[str]]

SSHClientKeys = typing.Optional[
    typing.Union[SSHKeySource, typing.Sequence[SSHKeySource]]
]

SSHKnownHosts = typing.Optional[
    typing.Union[SSHKeySource, typing.Sequence[SSHKeySource]]
]

PathnameConstructor = typing.Type[typing.Union[PurePath, Pathname]]

SSHShellSelection = typing.Union[ShellFlavourSelection, typing.Literal["auto"]]

SSHPathSelection = typing.Union[PathnameConstructor, typing.Literal["auto"]]


#: `connect_options` keys hostctl owns, and the field to use instead.
_OWNED_CONNECT_OPTIONS = {
    "host": "host",
    "port": "port",
    "username": "username",
    "password": "password",
    "client_keys": "client_keys",
    "known_hosts": "known_hosts",
    "config": "ssh_config",
    "connect_timeout": "connect_timeout",
}


#: Login shells hostctl knows how to render for. `"auto"` is resolved from
#: the dialect; the rest name what the server actually runs.
_LOGIN_SHELLS = frozenset(("auto", "cmd", "powershell", "posix"))


#: Characters OpenSSH's `valid_hostname` refuses, beyond whitespace and a
#: leading `-`. A host is substituted into a ProxyCommand (`%h`), which
#: OpenSSH runs through a shell (CVE-2023-51385).
_HOST_REFUSED = frozenset("'`\"$\\;&<>|(){}")

#: The same for `valid_ruser`, which keeps `\\`, `$` and spaces legal.
_USER_REFUSED = frozenset("'`\";&<>|(){}")


def _refuse_unsafe_target(host: str, username: typing.Optional[str]) -> None:
    """Refuse a host or username OpenSSH itself would refuse.

    netimps decides whether the host is an address literal or a well-formed
    name; the rest is the OpenSSH-specific list, which netimps does not know.
    """
    import netimps

    if host.startswith("-"):
        raise ValueError(
            "SSH host must be one host name, as OpenSSH requires: it cannot start with '-'"
        )
    for char in host:
        if char.isspace() or ord(char) < 32 or ord(char) == 127:
            raise ValueError(
                "SSH host must be one host name, as OpenSSH requires: it cannot contain whitespace or control characters"
            )
        if char in _HOST_REFUSED:
            raise ValueError(
                f"SSH host must be one host name, as OpenSSH requires: it cannot contain {char!r}"
            )
    if netimps.try_parse(host) is None and not netimps.is_valid(host, netimps.FQDN):
        raise ValueError("SSH host must be an address or a valid host name")
    if not username:
        return
    if username.startswith("-"):
        raise ValueError("SSH username cannot start with '-'")
    if "${" in username:
        raise ValueError("SSH username cannot contain '${'")
    if username.endswith("\\"):
        raise ValueError("SSH username cannot end with a backslash")
    for char in username:
        if char in _USER_REFUSED:
            raise ValueError(f"SSH username cannot contain {char!r}")
    for index, char in enumerate(username[:-1]):
        if char.isspace() and username[index + 1] == "-":
            raise ValueError("SSH username cannot have whitespace before '-'")


@dataclasses.dataclass
class SSHConfig(HostConfig, schemes=("ssh",)):
    """Explicit SSH transport, authentication, and target-shell settings."""

    #: Declared rather than enforced inside `_from_parsed_uri`, so the
    #: whitelist can be read before a config is built: an ambient
    #: credential (the CLI's `HOSTCTL_PASSWORD`) is offered only where it
    #: is accepted. The base dispatcher enforces it.
    uri_credentials = ("password", "client_keys", "known_hosts")

    host: str
    port: int = 22
    username: typing.Optional[str] = None
    password: typing.Optional[str] = dataclasses.field(default=None, repr=False)
    client_keys: SSHClientKeys = dataclasses.field(default=None, repr=False)
    executable: typing.Optional[str] = None
    known_hosts: SSHKnownHosts = ()
    dialect: SSHShellSelection = POSIX_SHELL
    path_flavor: SSHPathSelection = PosixPathname
    #: What parses the command string when it ARRIVES, before the command
    #: line's own shell runs.
    #:
    #: AsyncSSH sends one string and the server hands it to a login shell.
    #: On a POSIX server that is `$SHELL -c`; `"posix"` covers every sh-family
    #: shell and fish (csh and tcsh are unsupported), and the string is
    #: rendered so each reads it back verbatim. Windows OpenSSH's
    #: stock server hands it to `cmd.exe /c`, so a `cmd`-dialect command
    #: line (which already carries one cmd layer) is parsed by cmd TWICE:
    #: measured against a real cmd.exe, 4 of 8 adversarial values survived
    #: and two of the failures were command injection.
    #:
    #: `"auto"` reads the dialect: a Windows path flavour means the stock
    #: `cmd.exe`, anything else means a POSIX login shell. The server's
    #: shell is configurable (`HKLM\SOFTWARE\OpenSSH\DefaultShell`), so
    #: say so explicitly when it has been changed -- guessing is wrong in
    #: both directions.
    login_shell: str = "auto"
    #: Seconds asyncssh may spend establishing the connection (TCP, key
    #: exchange and authentication); `None` leaves asyncssh's own default.
    connect_timeout: typing.Optional[float] = 30.0
    #: An ssh_config to read on BOTH legs (`Host`, `HostName`, `IdentityFile`,
    #: `ProxyJump` ...). `None`, the default, reads none. One path: OpenSSH's
    #: `Include` covers the rest. Never part of the URI: a `ProxyCommand`
    #: starts a local process, which whoever wrote the URI must not choose.
    ssh_config: typing.Optional[typing.Union[str, "os.PathLike[str]"]] = None
    #: Extra keywords for `asyncssh.connect`, given to both legs. Never part
    #: of the URI or `repr()`, since it may hold a passphrase.
    connect_options: typing.Mapping[str, object] = dataclasses.field(
        default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.host:
            raise ValueError("host must not be empty")
        # The constructor takes a HOST; the dispatcher takes a URI.
        _refuse_uri("SSHConfig", self.host)
        _refuse_unsafe_target(self.host, self.username)
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.dialect != "auto":
            self.dialect = shell_flavour(self.dialect)
        elif self.executable is not None:
            raise ValueError("executable cannot be combined with dialect='auto'")
        self.path_flavor = path_flavor_selection(self.path_flavor, allow_auto=True)
        self.connect_options = dict(self.connect_options)
        for key in self.connect_options:
            if key in _OWNED_CONNECT_OPTIONS:
                raise ValueError(
                    f"connect_options cannot set {key!r}; "
                    f"use SSHConfig({_OWNED_CONNECT_OPTIONS[key]}=...)"
                )
        if self.connect_timeout is not None and not self.connect_timeout > 0:
            raise ValueError("connect_timeout must be greater than 0 seconds")
        if self.login_shell not in _LOGIN_SHELLS:
            values = ", ".join(sorted(_LOGIN_SHELLS))
            raise ValueError(f"login_shell must be one of: {values}")

    @property
    def resolved_login_shell(self) -> str:
        """`login_shell` with `"auto"` answered from the dialect.

        With `path_flavor="auto"` the answer needs the server: use the
        transport's `login_shell`.
        """
        if self.login_shell != "auto":
            return self.login_shell
        if self.path_flavor == "auto":
            raise ValueError("login_shell='auto' needs a known path_flavor")
        return "cmd" if issubclass(self.path_flavor, PureWindowsPath) else "posix"

    def connect_opts(self) -> typing.Dict[str, object]:
        opts: typing.Dict[str, object] = {}
        if self.username:
            # Left out otherwise, so asyncssh uses the local account name,
            # as `ssh` does.
            opts["username"] = self.username
        if self.password is not None:
            opts["password"] = self.password
        if self.client_keys is not None:
            keys = self.client_keys
            if isinstance(keys, (str, bytes, os.PathLike)):
                keys = [keys]
            opts["client_keys"] = keys
        # `config` is always passed: asyncssh would otherwise load
        # `~/.ssh/config`, and a library call must not depend on a personal
        # file. `ssh_config` is the explicit opt-in. Both legs get these
        # options, so they resolve and verify the host alike.
        opts["config"] = (
            None
            if self.ssh_config is None
            else [os.path.expanduser(os.fspath(self.ssh_config))]
        )
        # Always passed, never omitted on the default: `()` is asyncssh's own
        # "resolve known_hosts the usual way" value, and both legs receive
        # these options, so the path leg verifies the host key exactly as the
        # exec leg does. Opting out stays possible and explicit:
        # `known_hosts=None`.
        opts["known_hosts"] = self.known_hosts
        if self.connect_timeout is not None:
            opts["connect_timeout"] = self.connect_timeout
        opts.update(self.connect_options)
        return opts

    @property
    def connection_uri(self) -> str:
        user = f"{quote(self.username, safe='')}@" if self.username else ""
        query = {
            "dialect": self.dialect,
            "path_flavor": path_flavor_name(self.path_flavor),
        }
        if self.executable:
            query["executable"] = self.executable
        if self.login_shell != "auto":
            # Only when it was said: `auto` is the default and carrying it
            # would change every existing URI's text.
            query["login_shell"] = self.login_shell
        if self.connect_timeout != 30.0:
            query["connect_timeout"] = self.connect_timeout
        from netimps import join_host

        return f"ssh://{user}{join_host(self.host, self.port)}?{urlencode(query)}"

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> SSHConfig:
        query = strict_uri_query(
            parsed,
            {
                "dialect",
                "path_flavor",
                "executable",
                "login_shell",
                "connect_timeout",
            },
        )
        if not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("SSH URI requires a host and no path")
        from netimps import get_default_port

        return cls(
            host=uri_hostname(parsed),
            port=get_default_port("ssh") if parsed.port is None else parsed.port,
            username=unquote(parsed.username or "") or None,
            password=typing.cast(typing.Optional[str], credentials.get("password")),
            client_keys=typing.cast(SSHClientKeys, credentials.get("client_keys")),
            executable=query.get("executable") or None,
            known_hosts=typing.cast(SSHKnownHosts, credentials.get("known_hosts", ())),
            dialect=(
                "auto"
                if query.get("dialect") == "auto"
                else shell_flavour(query.get("dialect", POSIX_SHELL.name))
            ),
            path_flavor=path_flavor_selection(
                query.get("path_flavor", "posix"), allow_auto=True
            ),
            login_shell=query.get("login_shell", "auto"),
            connect_timeout=(
                float(query["connect_timeout"]) if "connect_timeout" in query else 30.0
            ),
        )

    def _create_host(self):
        from ._providers import SFTPPathProvider
        from ._providers import SSHExecutorProvider

        from ...system._kinds import PosixHost, WindowsHost

        transport = self._create_transport()
        try:
            # "auto" connects while the host is created, because the host
            # class depends on what the server answers.
            flavor = transport.path_flavor
        except BaseException:
            transport.close()
            raise
        host_type = WindowsHost if issubclass(flavor, PureWindowsPath) else PosixHost
        return host_type(
            self,
            executor_providers=(SSHExecutorProvider(transport),),
            path_providers=(SFTPPathProvider(transport),),
            shell=(
                self.dialect
                if self.dialect != "auto"
                else (lambda: transport.shell_flavour)
            ),
        )

    def _create_transport(self) -> _SSHTransport:
        """Create the private SSH service shared by executor/path providers."""
        from ._transport import _SSHTransport

        return _SSHTransport(self)
