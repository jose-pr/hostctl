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

## Local

| | |
| --- | --- |
| URI | `local:` |
| class | `LocalConfig()` |
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
| query keys | `shell`, `pathname`, `executable`, `login_shell`, `connect_timeout` |
| credentials | `password`, `client_keys`, `known_hosts` |
| keyword only | `ssh_config`, `connect_options` (never in a URI) |
| extra | `ssh` |

```python
from hostctl import HostConfig, SSHConfig

config = SSHConfig(
    "nas.example",
    port=2222,
    username="admin",
    client_keys="~/.ssh/id_ed25519",
    known_hosts="~/.ssh/known_hosts",
    connect_timeout=10.0,
)
same = HostConfig(
    "ssh://admin@nas.example:2222?connect_timeout=10",
    client_keys="~/.ssh/id_ed25519",
    known_hosts="~/.ssh/known_hosts",
)
assert str(same) == str(config)
```

- **Account.** With no `username`, asyncssh uses the local account name, as
  `ssh` does.
- **Host keys.** `known_hosts=()` (the default) verifies the server's key
  against asyncssh's usual known-hosts files; a path, bytes or a sequence of
  them names the trusted keys; `known_hosts=None` turns verification off. The
  exec and SFTP connections verify the same way.
- **ssh_config.** Neither connection reads `~/.ssh/config` unless
  `ssh_config=` names a file. It cannot come from a URI, because a
  `ProxyCommand` starts a local process.
- **Timeouts.** `connect_timeout` (default 30 s) bounds connecting only;
  `run(timeout=)` bounds a command.
- Opening it returns a `PosixHost`, or a `WindowsHost` when `pathname` is
  Windows. For a Windows OpenSSH server pass `shell=POWERSHELL` (or `CMD`) and
  `pathname="windows"`; `shell="auto"` and `pathname="auto"` probe instead of
  guessing.

## WinRM

| | |
| --- | --- |
| URI | `winrm://user@host[:port][?query]` (HTTP), `winrms://...` (HTTPS) |
| query keys | `auth`, `verify`, `message_encryption`, `operation_timeout`, `read_timeout`, `timeout`, `provider`, `cleartext_auth` |
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

- **TLS.** `winrms://` (or `tls=True`) is HTTPS; `verify=False`
  (`?verify=false`) skips the server certificate check.
- **Cleartext.** `auth="basic"`, `"plaintext"` or `"ssl"` over plain HTTP
  would send the password in cleartext, so it raises `ValueError` unless
  `cleartext_auth=True` (`?cleartext_auth=true`). NTLM (the default),
  Kerberos and CredSSP encrypt the message.
- **No password.** On a Windows client, a config with no password uses the
  current Windows login (native PowerShell remoting); `username` must name that
  user.
- **Timeouts.** All are float seconds. `timeout` (default 30) is the default
  for `run(timeout=)`: the same total deadline per command, which a per-call
  `timeout=` overrides; `timeout=None` (`?timeout=`) sets none, so a command
  that never finishes blocks the call, and on the native leg so does a host
  that stops answering once the session is open. `operation_timeout` and
  `read_timeout` bound one WS-Man request each, as SSH's `connect_timeout`
  bounds only connecting; on the native leg `read_timeout` bounds opening the
  session.

## Containers

| | |
| --- | --- |
| URI | `docker://container[?query]` |
| query keys | `engine_url`, `username`, `workdir`, `executable`, `shell`, `pathname` |
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
it is `docker.DockerClient(base_url=engine_url)`. For anything else -- TLS to
a remote engine, a pre-built client -- pass `client_factory=`.

## QEMU guests

| | |
| --- | --- |
| URI, local libvirt | `qemu+libvirt:///domain[?connection=LIBVIRT_URI]` |
| URI, Unix socket | `qga+unix:///SOCKET_PATH?domain=DOMAIN` (the path percent-encoded) |
| URI, socket over SSH | `qga+ssh://user@hypervisor/domain[?socket_path=PATH]` |
| query keys | `agent_timeout`, `max_reply_size`, `shell`, `pathname`, plus the ones above |
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
`agent_timeout` bounds one guest-agent round trip, not a command.

## Serial consoles

| | |
| --- | --- |
| URI | `serial:///dev/ttyUSB0`, `serial:///COM3`, `serial:///loop://` (`[?query]`) |
| query keys | `baudrate`, `bytesize`, `parity`, `stopbits`, `xonxoff`, `rtscts`, `dsrdtr`, `read_timeout`, `write_timeout`, `inter_byte_timeout`, `exclusive` |
| credentials | `profile`, `serial_factory`, `serial_port` |
| extra | `serial` (unless a port is injected) |

```python
from hostctl import HostConfig, PromptConsoleProfile, SerialConfig

windows_port = HostConfig("serial:///COM3?baudrate=9600")
posix_port = HostConfig("serial:///dev/ttyUSB0")
assert posix_port.device == "/dev/ttyUSB0"
assert windows_port.device == "COM3"
network_port = SerialConfig("rfc2217://console.example:4001")

with_login = SerialConfig(
    "COM3",
    profile=PromptConsoleProfile(
        "$ ",
        login=[("login: ", "admin"), ("Password: ", "secret")],
    ),
)
```

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
encrypted.

## System URIs

`posix://`, `windows://` and `ios://` describe the system and name its
providers instead of owning a transport: `executor` and `path` are repeatable
query keys naming registered providers (`local`, `ssh`, `sftp`, `winrm`, or
your own). A transport provider takes its config, credentials included, from
`provider_options`, which is never rendered into the URI.

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
