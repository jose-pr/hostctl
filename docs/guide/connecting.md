# Connecting

Every target is described by a **config**: the transport's settings, without a
live connection. A config is built in one of two ways, and both give the same
object:

- **by class**, `SSHConfig("nas.example", username="admin")`: the target is
  the one positional argument, everything else is a keyword (a second
  positional argument raises `TypeError`);
- **by URI**, `HostConfig("ssh://admin@nas.example")`, which picks the config
  class from the scheme. `Host(uri)` does the same and returns the host.

`config.open()` creates the host, and `with config as host:` connects and
always closes. Nothing connects before that.

## Credentials

Secrets never live in the URI a config renders. They reach a config three
ways:

- as keyword arguments to the class, or as `**secrets` to
  `HostConfig(uri, **secrets)`/`Host(uri, **secrets)`;
- as the password in a URI's userinfo, `ssh://admin:secret@nas.example`. It is
  accepted on the way in, moved into the credentials, and never rendered back
  out: `str(config)` and `config.connection_uri` carry no password or key.
  Giving the password in both places raises `ValueError`;
- as extra lines of the password field, split by `parse_credentials`: the
  first line is the password and each later `name:value` line is passed as the
  credential keyword `name`, so a second factor travels in the same field.

Each config names the credentials it accepts (`uri_credentials`); any other
keyword raises `ValueError` naming it, so a typo such as `passwrd=` never
builds a config without a password.

```python
from hostctl import HostConfig, SSHConfig, parse_credentials

config = HostConfig("ssh://admin:secret@nas.example")
assert isinstance(config, SSHConfig)
assert "secret" not in str(config) and "secret" not in repr(config)

assert parse_credentials("hunter2\notp:123456") == ("hunter2", {"otp": "123456"})

try:
    HostConfig("ssh://admin@nas.example", passwrd="secret")
except ValueError as error:
    print(error)  # unknown credential argument: passwrd
```

Percent-encode `/`, `?` and `#` in a password written into a URI; `@` and `:`
parse as written. `redact_uri(uri)` strips the password from a URI for a log.

## Timeouts

Every config takes `timeout=`, spelled the same way everywhere. A number or
`None` is the **total**: the default deadline of `run(timeout=)`, `None`
meaning no limit. A `Timeout` names the other fields too:

```python
from hostctl import DEFAULT, HostConfig, SSHConfig, Timeout

config = SSHConfig("nas.example", timeout=Timeout(600, connect=10))
assert config.timeout == Timeout(total=600, connect=10)
assert SSHConfig("nas.example", timeout=600).timeout == Timeout(total=600)

same = HostConfig("ssh://nas.example?timeout=600&timeout_connect=10")
assert same.timeout == config.timeout

# What each DEFAULT became: SSH connects within 30 s unless told otherwise.
assert SSHConfig("nas.example").timeouts == Timeout(total=None, connect=30)
assert Timeout().read is DEFAULT
```

| field | bounds | URI key |
| --- | --- | --- |
| `total` | a whole command; the default of `run(timeout=)` | `timeout` |
| `connect` | establishing a connection, session or port | `timeout_connect` |
| `read` | one reply, or one read | `timeout_read` |
| `write` | one write | `timeout_write` |

Each field is `DEFAULT` (the transport's own), `None` (no limit) or a positive
number of seconds; a serial `read` or `write` may also be `0`, pyserial's
"do not wait" (every other transport refuses a zero). In a URI an empty value is `None` and an absent key is
`DEFAULT`, and `str(config)` writes only the fields that are not `DEFAULT`.
A transport supports only some fields, and setting one it cannot honour raises
`ValueError` naming the config and the field rather than being ignored:

| config | `total` | `connect` | `read` | `write` |
| --- | --- | --- | --- | --- |
| Local | none | -- | -- | -- |
| SSH | none | 30 s | -- | -- |
| WinRM | 30 s | 30 s | -- | -- |
| Container | -- | 60 s | -- | -- |
| QEMU guest | none | the `read` value (unix and ssh transports) | 10 s | -- |
| Serial | none | -- | 0.1 s | 10 s |
| System URIs | the provider's own | -- | -- | -- |

`run(timeout=)` overrides the total for one call, and `run(timeout=None)`
lifts it, WinRM's 30 s included. The per-transport sections below say what
each field bounds there; the
[API header](https://github.com/jose-pr/hostctl/blob/main/src/hostctl/AGENTS.md)
has the full rules.

## Local

| | |
| --- | --- |
| URI | `local:` (`local:?timeout=60` with a total) |
| class | `LocalConfig()` |
| timeouts | `total` |
| extra | none |

```python
from hostctl import LocalConfig

with LocalConfig() as host:
    print(host.run(["python", "-c", "print('hello')"], encoding="utf-8").stdout)
```

## SSH

| | |
| --- | --- |
| URI | `ssh://[user@]host[:port][?query]` |
| query keys | `shell`, `pathname`, `executable`, `login_shell`, `verify`, and the timeout keys |
| credentials | `password`, `client_keys`, `known_hosts` |
| keyword only | `ssh_config`, `connect_options` (never in a URI) |
| extra | `ssh` |

```python
from hostctl import HostConfig, SSHConfig, Timeout

config = SSHConfig(
    "nas.example",
    port=2222,
    username="admin",
    client_keys="~/.ssh/id_ed25519",
    known_hosts="~/.ssh/known_hosts",
    timeout=Timeout(connect=10.0),
)
same = HostConfig(
    "ssh://admin@nas.example:2222?timeout_connect=10",
    client_keys="~/.ssh/id_ed25519",
    known_hosts="~/.ssh/known_hosts",
)
assert str(same) == str(config)
```

- **Account.** With no `username`, asyncssh uses the local account name, as
  `ssh` does.
- **Host keys.** `known_hosts=()` (the default) verifies the server's key
  against asyncssh's usual known-hosts files; a path, bytes or a sequence of
  them names the trusted keys. `verify=False` (`?verify=false`) is the one
  way to skip the check: it replaces the usual lookup, while a `known_hosts`
  you name is still checked. `known_hosts=None` raises, and so does an
  `ssh_config` with `UserKnownHostsFile none` unless `verify=False`. The exec
  and SFTP connections verify the same way.
- **ssh_config.** Neither connection reads `~/.ssh/config` unless
  `ssh_config=` names a file. It cannot come from a URI, because a
  `ProxyCommand` starts a local process.
- **Timeouts.** `timeout.connect` (default 30 s) bounds connecting only:
  TCP, key exchange and authentication; `None` leaves asyncssh's own. The
  total (no default) bounds a command. `read` and `write` raise.
- Opening it returns a `PosixHost`, or a `WindowsHost` when `pathname` is
  Windows. For a Windows OpenSSH server pass `shell=POWERSHELL` (or `CMD`) and
  `pathname="windows"`; `shell="auto"` and `pathname="auto"` probe instead of
  guessing.

## WinRM

| | |
| --- | --- |
| URI | `winrm://user@host[:port][?query]` (HTTP), `winrms://...` (HTTPS) |
| query keys | `auth`, `verify`, `message_encryption`, `provider`, `cleartext_auth`, and the timeout keys |
| credentials | `password` |
| extra | `winrm`; `psrp` for runspaces (Python 3.10+); `winrm-credssp`, `winrm-kerberos` for those `auth` values |

```python
from hostctl import HostConfig, WinRMConfig

config = WinRMConfig(
    "win.example", username="admin", password="secret", tls=True, verify=False
)
same = HostConfig("winrms://admin@win.example?verify=false", password="secret")
assert str(same) == str(config)
assert "secret" not in str(config)
```

- **TLS.** `winrms://` (or `tls=True`) is HTTPS. The certificate is checked
  against the Windows certificate store on the native leg and against
  `requests`' default (`REQUESTS_CA_BUNDLE`, else certifi) on pywinrm and
  PSRP. `verify="ca.pem"` (`?verify=ca.pem`, a file or a hashed directory)
  trusts that bundle instead, on pywinrm and PSRP; the native leg refuses one,
  since PowerShell remoting trusts only the store. `verify=False`
  (`?verify=false`) skips every certificate check.
- **Cleartext.** `auth="basic"`, `"plaintext"` or `"ssl"` over plain HTTP
  would send the password in cleartext, so it raises `ValueError` unless
  `cleartext_auth=True` (`?cleartext_auth=true`). NTLM (the default),
  Kerberos and CredSSP encrypt the message.
- **No password.** On a Windows client, a config with no password uses the
  current Windows login (native PowerShell remoting); `username` must name that
  user.
- **Timeouts.** Two fields, both 30 s by default. The total is the default
  for `run(timeout=)`: the same total deadline per command, which a
  per-call `timeout=` overrides; `timeout=None` (`?timeout=`) sets none, so
  a command that never finishes blocks the call, and on the native leg so
  does a host that stops answering once the session is open.
  `timeout.connect` bounds only establishing the connection, as on SSH, and
  a host that never accepts it raises `ConnectionError`.
  `Timeout(connect=None)` (`?timeout_connect=`) keeps each leg's library
  default: PowerShell's 180 s on the native leg, pypsrp's 30 s on PSRP, and
  on pywinrm its single 30 s HTTP timeout, which then bounds connecting too.
  How long one WS-Man request may wait is fixed and not configurable, so
  `read` and `write` raise.

## Containers

| | |
| --- | --- |
| URI | `docker://container[?query]` |
| query keys | `engine_url`, `verify`, `username`, `workdir`, `executable`, `shell`, `pathname`, `timeout_connect` |
| credentials | `client_factory` (a callable returning a Docker client) |
| extra | `container` |

```python
from hostctl import ContainerConfig, HostConfig

config = ContainerConfig("application", username="app", workdir="/srv")
same = HostConfig("docker://application?username=app&workdir=/srv")
assert str(same) == str(config)
```

With no `engine_url` the Docker client comes from the environment
(`docker.from_env()`, which reads `DOCKER_HOST` and its TLS settings); with one
it is `docker.DockerClient(base_url=engine_url)`. An `https://` engine URL is
TLS: `verify="ca.pem"` trusts that CA bundle instead of docker-py's default,
`verify=False` checks nothing, and either holds even when
`REQUESTS_CA_BUNDLE` is set. For anything else -- a client certificate, a
pre-built client -- pass `client_factory=` (and leave `verify` alone).
`timeout.connect` is the client's request timeout (the SDK's 60 s by
default), passed as `timeout=` to the client or the factory only when set;
it bounds connecting to the Engine and each API reply. A container has no
total: an Engine exec cannot be cancelled, so `timeout=<n>` raises and so
does `run(timeout=<n>)`.

## QEMU guests

| | |
| --- | --- |
| URI, local libvirt | `qemu+libvirt:///domain[?connection=LIBVIRT_URI]` |
| URI, Unix socket | `qga+unix:///SOCKET_PATH?domain=DOMAIN` (the path percent-encoded) |
| URI, socket over SSH | `qga+ssh://user@hypervisor/domain[?socket_path=PATH&verify=false]` |
| query keys | `max_reply_size`, `shell`, `pathname`, the timeout keys, plus the ones above |
| credentials | `transport_factory`, `serial_console`, `path_helper`; on `qga+ssh`, also `password`, `client_keys`, `known_hosts` |
| extra | `qemu-libvirt` for libvirt; `ssh` for `qga+ssh` |

```python
from hostctl import HostConfig, QEMUConfig, SSHConfig

local = HostConfig("qemu+libvirt:///vm-id?connection=qemu:///system")
socket = HostConfig("qga+unix:///%2Frun%2Fqga.sock?domain=vm-id")
assert socket.socket_path == "/run/qga.sock"

tunneled = QEMUConfig(
    "vm-id",
    transport="ssh",
    ssh=SSHConfig("hypervisor.example", username="root"),
)
assert isinstance(HostConfig(str(tunneled)), QEMUConfig)
```

The SSH tunnel takes an `SSHConfig`, so its host keys follow the SSH rules
above; `qga+ssh` defaults `socket_path` to `/run/qemu-server/DOMAIN.qga`.
`timeout.read` (10 s) bounds one guest-agent round trip, not a command;
`timeout.connect` bounds opening a unix or ssh agent stream (by default it
shares the first reply's deadline) and is refused on libvirt, which opens its
own; the total bounds a command. The tunnel's SSH connection itself follows
its `SSHConfig`'s `timeout.connect`.

## Serial consoles

| | |
| --- | --- |
| URI | `serial:///dev/ttyUSB0`, `serial:///COM3`, `serial:///loop://` (`[?query]`) |
| query keys | `baudrate`, `bytesize`, `parity`, `stopbits`, `xonxoff`, `rtscts`, `dsrdtr`, `inter_byte_timeout`, `exclusive`, and the timeout keys |
| credentials | `profile`, `serial_factory`, `serial_port` |
| extra | `serial` (unless a port is injected) |

```python
from hostctl import HostConfig, PosixConsoleProfile, PromptConsoleProfile, SerialConfig

windows_port = HostConfig("serial:///COM3?baudrate=9600")
posix_port = HostConfig("serial:///dev/ttyUSB0")
assert posix_port.device == "/dev/ttyUSB0"
assert windows_port.device == "COM3"
network_port = SerialConfig("rfc2217://console.example:4001")

linux_console = SerialConfig(
    "COM3",
    profile=PosixConsoleProfile(login=[("login: ", "root"), ("Password: ", "secret")]),
)
switch_console = SerialConfig(
    "COM4",
    profile=PromptConsoleProfile(
        "switch# ",
        login=[("Username: ", "admin"), ("Password: ", "secret")],
    ),
)
```

A Linux or other POSIX shell on the line takes `PosixConsoleProfile`, which
frames output, stderr and the exit status without reading the echo; a network
device's console takes `PromptConsoleProfile` (see
[Running commands](run.md)).

The device reads the way a `file:` URI's path does (RFC 8089):
`serial:///dev/ttyUSB0` and `serial:/dev/ttyUSB0` are `/dev/ttyUSB0`. A
device pyserial resolves by name rather than as a path drops the URI's leading
slash, as `file:///C:/x` is `C:/x`: a Windows port (`serial:///COM3` is
`COM3`, `serial:///%5C%5C.%5CCOM10` is `\\.\COM10`) and a pyserial URL
(`serial:///loop://` and `serial:///loop%3A%2F%2F` are both `loop://`). A
relative device is the opaque form, `serial:ttyUSB0`. Percent-encode only what
a URI path cannot hold (a space, `?`, `#`); an encoded slash right after the
leading one is part of the device, so `serial:///%2Fdev%2FttyUSB0` is still
`/dev/ttyUSB0`. `serial:////dev/ttyUSB0` is refused, naming the three-slash
spelling. `SerialConfig(device)` takes the device as written, and
`str(config)` renders the readable form that reads back as the same device. A
console has no
username or password of its own; a login is the profile's `login=` steps,
whose `secret=True` values are redacted from transcripts. RFC 2217 is not
encrypted. `timeout.read` (0.1 s) and `timeout.write` (10 s) are pyserial's
read and write timeouts; `inter_byte_timeout` stays a field of its own,
since it is a framing gap within one read rather than a bound on one.

## System URIs

`posix://`, `windows://` and `ios://` describe the system and name its
providers instead of owning a transport: `executor` and `path` are repeatable
query keys naming registered providers (`local`, `ssh`, `sftp`, `winrm`, or
your own). A transport provider takes its config, credentials included, from
`provider_options`, which is never rendered into the URI. A system config's
`timeout=` sets only the total (`?timeout=`); left `DEFAULT`, the provider
applies its own (a WinRM provider its config's 30 s), and connecting follows
the transport config in `provider_options`.

```python
from hostctl import HostConfig, PosixHost, SSHConfig

config = HostConfig(
    "posix://nas.example?executor=ssh&path=sftp",
    provider_options={"ssh": SSHConfig("nas.example", username="admin")},
)
host = config.open()
assert isinstance(host, PosixHost)
host.close()

local = HostConfig("posix://localhost?executor=local&path=local")
```

[Systems and providers](providers.md) covers provider selection, and
[Extending hostctl](extending.md) adding a scheme of your own.
