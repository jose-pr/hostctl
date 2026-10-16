"""SSH configuration: target, credentials, policy and shell selection."""

from __future__ import annotations

import dataclasses
import os
import typing
from pathlib import PureWindowsPath
from urllib.parse import quote, unquote, urlencode

from pathapi import PosixPathname


from ...host._config import HostConfig
from ...host._dispatch import _keyword_only
from ...host._selection import PathnameSelection, ShellSelection
from ...host._timeout import (
    _URI_TIMEOUT_KEYS,
    _TimeoutField,
    _refuse_renamed_keys,
    _timeout_from_query,
    _timeout_query,
)
from ...host._verify import _host_key_verify, _verify_from_query
from ...uri._parse import (
    _refuse_uri,
    pathname_label,
    pathname_selection,
    strict_uri_query,
    uri_hostname,
)
from ...shell import POSIX_SHELL, shell_flavour

if typing.TYPE_CHECKING:
    from ._transport import _SSHTransport


SSHKeySource = typing.Union[str, bytes, os.PathLike[str]]

SSHClientKeys = typing.Optional[
    typing.Union[SSHKeySource, typing.Sequence[SSHKeySource]]
]

#: No `None`: skipping the host-key check is `SSHConfig(verify=False)`.
SSHKnownHosts = typing.Union[SSHKeySource, typing.Sequence[SSHKeySource]]


#: `connect_options` keys hostctl owns, and the keyword to use instead.
_OWNED_CONNECT_OPTIONS = {
    "host": "host=...",
    "port": "port=...",
    "username": "username=...",
    "password": "password=...",
    "client_keys": "client_keys=...",
    "known_hosts": "known_hosts=...",
    "config": "ssh_config=...",
    "connect_timeout": "timeout=Timeout(connect=...)",
}


#: Login shells hostctl knows how to render for. `"auto"` is resolved from
#: the shell; the rest name what the server actually runs.
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


@_keyword_only()
@dataclasses.dataclass
class SSHConfig(HostConfig, schemes=("ssh",)):
    """Explicit SSH transport, authentication, and target-shell settings."""

    #: Declared rather than enforced inside `_from_parsed_uri`, so the
    #: whitelist can be read before a config is built: an ambient
    #: credential (the CLI's `HOSTCTL_PASSWORD`) is offered only where it
    #: is accepted. The base dispatcher enforces it.
    uri_credentials = ("password", "client_keys", "known_hosts")

    _timeout_defaults = {"total": None, "connect": 30.0}

    host: str
    port: int = 22
    username: typing.Optional[str] = None
    password: typing.Optional[str] = dataclasses.field(default=None, repr=False)
    client_keys: SSHClientKeys = dataclasses.field(default=None, repr=False)
    executable: typing.Optional[str] = None
    #: `True` checks the server's host key; `False` is the one way to skip
    #: the check. SSH has no CA bundle, so a string is refused: name trusted
    #: keys with `known_hosts`.
    verify: bool = True
    #: The trusted host keys. `()` is the usual lookup (`~/.ssh/known_hosts`,
    #: or an `ssh_config`'s `UserKnownHostsFile`), which `verify=False`
    #: switches off; anything else is checked even under `verify=False`.
    known_hosts: SSHKnownHosts = ()
    shell: ShellSelection = POSIX_SHELL
    pathname: PathnameSelection = PosixPathname
    #: What parses the command string when it ARRIVES, before the command
    #: line's own shell runs.
    #:
    #: AsyncSSH sends one string and the server hands it to a login shell.
    #: On a POSIX server that is `$SHELL -c`; `"posix"` covers every sh-family
    #: shell and fish (csh and tcsh are unsupported), and the string is
    #: rendered so each reads it back verbatim. Windows OpenSSH's
    #: stock server hands it to `cmd.exe /c`, so a `cmd`-shell command
    #: line (which already carries one cmd layer) is parsed by cmd TWICE:
    #: measured against a real cmd.exe, 4 of 8 adversarial values survived
    #: and two of the failures were command injection.
    #:
    #: `"auto"` reads the shell: a Windows path flavour means the stock
    #: `cmd.exe`, anything else means a POSIX login shell. The server's
    #: shell is configurable (`HKLM\SOFTWARE\OpenSSH\DefaultShell`), so
    #: say so explicitly when it has been changed -- guessing is wrong in
    #: both directions.
    login_shell: str = "auto"
    #: `total` is the default of `run(timeout=)` (none); `connect` bounds
    #: TCP, key exchange and authentication (30 s), and `None` there leaves
    #: asyncssh's own default.
    timeout: _TimeoutField = _TimeoutField()
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
        self.verify = _host_key_verify("SSHConfig", self.verify)
        if self.known_hosts is None:
            raise ValueError(
                "SSHConfig: known_hosts names the trusted host keys; "
                "pass verify=False to skip the host-key check"
            )
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.shell != "auto":
            self.shell = shell_flavour(self.shell)
        elif self.executable is not None:
            raise ValueError("executable cannot be combined with shell='auto'")
        self.pathname = pathname_selection(self.pathname, allow_auto=True)
        self.connect_options = dict(self.connect_options)
        for key in self.connect_options:
            if key in _OWNED_CONNECT_OPTIONS:
                raise ValueError(
                    f"connect_options cannot set {key!r}; "
                    f"use SSHConfig({_OWNED_CONNECT_OPTIONS[key]})"
                )
        if self.login_shell not in _LOGIN_SHELLS:
            values = ", ".join(sorted(_LOGIN_SHELLS))
            raise ValueError(f"login_shell must be one of: {values}")

    @property
    def resolved_login_shell(self) -> str:
        """`login_shell` with `"auto"` answered from the shell.

        With `pathname="auto"` the answer needs the server: use the
        transport's `login_shell`.
        """
        if self.login_shell != "auto":
            return self.login_shell
        if self.pathname == "auto":
            raise ValueError("login_shell='auto' needs a known pathname")
        return "cmd" if issubclass(self.pathname, PureWindowsPath) else "posix"

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
        # exec leg does. `None` (no check) comes only from `verify=False`.
        if self.known_hosts == () and not self.verify:
            opts["known_hosts"] = None
        else:
            opts["known_hosts"] = self.known_hosts
            if self.known_hosts == () and self.ssh_config is not None:
                self._refuse_unchecked_config(opts)
        connect = self.timeouts.connect
        if connect is not None:
            opts["connect_timeout"] = connect
        opts.update(self.connect_options)
        return opts

    def _refuse_unchecked_config(self, opts: typing.Mapping[str, object]) -> None:
        """Raise when the ssh_config would switch the host-key check off.

        `UserKnownHostsFile none` resolves `known_hosts=()` to asyncssh's
        "no check", which only `verify=False` may ask for. Resolving reads the
        file (and runs any `Match exec`) once more, without connecting.
        """
        from ... import _async

        resolved = (
            _async.asyncssh()
            .SSHClientConnectionOptions(
                config=opts["config"],
                host=self.host,
                port=self.port,
                username=self.username or (),
                known_hosts=(),
                client_keys=None,
                agent_path=None,
            )
            .known_hosts
        )
        if resolved is None:
            raise ValueError(
                "SSHConfig: the ssh_config sets UserKnownHostsFile none for "
                f"{self.host}; pass verify=False to skip the host-key check, "
                "or name the file with known_hosts="
            )

    @property
    def connection_uri(self) -> str:
        user = f"{quote(self.username, safe='')}@" if self.username else ""
        query = {
            "shell": self.shell,
            "pathname": pathname_label(self.pathname),
        }
        if self.executable:
            query["executable"] = self.executable
        if self.login_shell != "auto":
            # Only when it was said: `auto` is the default and carrying it
            # would change every existing URI's text.
            query["login_shell"] = self.login_shell
        if not self.verify:
            query["verify"] = "false"
        from netimps import join_host

        pairs = list(query.items()) + _timeout_query(self.timeout)
        return f"ssh://{user}{join_host(self.host, self.port)}?{urlencode(pairs)}"

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> SSHConfig:
        query = strict_uri_query(
            parsed,
            {
                "shell",
                "pathname",
                "executable",
                "login_shell",
                "verify",
                "connect_timeout",
                *_URI_TIMEOUT_KEYS,
            },
        )
        _refuse_renamed_keys(query, {"connect_timeout": "timeout_connect"})
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
            verify=typing.cast(bool, _verify_from_query(query, bundle=False)),
            known_hosts=typing.cast(SSHKnownHosts, credentials.get("known_hosts", ())),
            shell=(
                "auto"
                if query.get("shell") == "auto"
                else shell_flavour(query.get("shell", POSIX_SHELL.name))
            ),
            pathname=pathname_selection(
                query.get("pathname", "posix"), allow_auto=True
            ),
            login_shell=query.get("login_shell", "auto"),
            timeout=_timeout_from_query(query),
        )

    def _create_host(self):
        from ._providers import SFTPPathProvider
        from ._providers import SSHExecutorProvider

        from ...system._kinds import PosixHost, WindowsHost

        transport = self._create_transport()
        try:
            # "auto" connects while the host is created, because the host
            # class depends on what the server answers.
            flavor = transport.pathname
        except BaseException:
            transport.close()
            raise
        host_type = WindowsHost if issubclass(flavor, PureWindowsPath) else PosixHost
        return host_type(
            self,
            executor_providers=(SSHExecutorProvider(transport),),
            path_providers=(SFTPPathProvider(transport),),
            shell=(
                self.shell
                if self.shell != "auto"
                else (lambda: transport.shell_flavour)
            ),
        )

    def _create_transport(self) -> _SSHTransport:
        """Create the private SSH service shared by executor/path providers."""
        from ._transport import _SSHTransport

        return _SSHTransport(self)
