# `hostctl` — public API header

Header-file-style reference for the `hostctl` package: every public export with
its signature, arguments, contract and gotchas, so the package can be used
without reading its source. It ships inside the package and is
self-contained. Development documentation lives with the source at
<https://github.com/jose-pr/hostctl>.

Names are imported from `hostctl`, which re-exports the stable surface (hosts
and configs you construct, exceptions you catch, types you annotate with), or
from the public module that is each name's home: `hostctl.host`,
`hostctl.exceptions`, `hostctl.uri`, `hostctl.system`, `hostctl.provider`,
`hostctl.executor`, `hostctl.process`, `hostctl.paths`, `hostctl.shell`,
`hostctl.sync`, `hostctl.deploy`, `hostctl.console`, `hostctl.cli` and `hostctl.transports.local`, `.ssh`,
`.winrm`, `.container`, `.qemu`, `.serial`. Every public module declares
`__all__`; a name outside it, and every module with a `_`-prefixed component,
is internal and may change without notice. Concrete executors, process
adapters, path backends and backend path classes are internal: a host hands
them back typed as `pathapi.Path`, `Process` or `Executor`. `LocalExecutor` is
the one exported executor, because a caller constructs it directly to compose
a host out of providers without a transport.

`pip install hostctl` installs the core, which needs only `pathapi` and
`netimps`; each transport is an extra, `pip install "hostctl[ssh,winrm]"`.
Importing a transport package whose extra is missing works, and the first use
raises `MissingExtraError` (an `ImportError`) naming the extra to install. `hostctl.has_ssh()` and
`hostctl.has_psrp()` answer whether the `ssh` and `psrp` extras are
installed, importing nothing.

| extra | adds |
| --- | --- |
| `ssh` | SSH `run()`/`spawn()` (asyncssh) and the SFTP path leg (sftppath) |
| `winrm` | WinRM command execution and paths over pywinrm (basic, plaintext, ssl, NTLM) |
| `psrp` | PowerShell Remoting runspaces over pypsrp; Python 3.10+ only |
| `container` | Docker Engine containers: exec, sessions, archive-backed paths |
| `serial` | native, RFC 2217, socket and virtual serial ports (pyserial) |
| `qemu-libvirt` | local libvirt domain lookup for the QEMU guest agent |
| `winrm-credssp` | CredSSP authentication for pywinrm |
| `winrm-kerberos` | Kerberos authentication for pywinrm |
| `cli` | the `hostctl` command line (duho) |
| `deploy` | `hostctl.deploy`: bundling and installing Python programs (packaging, pathspec; tomli before 3.11) |

`hostctl.__version__` is the installed distribution's version string.

## Hosts and configs (`hostctl.host`)

A config is the target and how to reach it; a host is the open connection.
The URI form, keywords and credentials of every transport's config are in the
Connecting guide, <https://jose-pr.github.io/hostctl/guide/connecting/>.
Every config takes its target positionally and everything else by keyword: a
second positional argument raises `TypeError`, so an SSH port is always
`port=2222` (a static type checker still reads each dataclass's generated
`__init__`, which shows the fields as positional).

```python
Host(connection_string, /, **secrets)
HostConfig(connection_string, /, **secrets)
```

- `Host(uri, **secrets)` returns a host; `HostConfig(uri, **secrets)` returns
  the config of the scheme without creating one. Both dispatch on the URI
  scheme through the registered configs and the `hostctl.configs` entry-point
  group. A narrowed parse is `config = HostConfig(uri);
  isinstance(config, SSHConfig)`.
- `str(config)` and `config.connection_uri` are the canonical, secret-free
  connection string, which can be passed back to `HostConfig`;
  `config.scheme` is its URI scheme. Neither ever contains a password or a
  private key.
- A `scheme://user:password@host` URI is valid input: the password moves into
  the credential arguments and leaves the parsed authority, so it never
  reaches a field that `connection_uri` or `repr()` renders. The password field is split
  by `parse_credentials` (see "URIs"), so a second factor travels in it; an
  extra the config does not declare is rejected by name.
- Keyword credentials override the URI's, so one URI serves several logins:
  `password=` replaces the URI's whole password field (its extras included),
  `password=""` means no password, and `password=None` keeps the URI's.
  `username=` replaces the URI's user (or adds one) on the schemes whose
  config logs in as a user -- `ssh`, `winrm`/`winrms` and `qga+ssh`; any
  other scheme raises `ValueError` for it, as for every credential it does
  not take. `connection_uri` shows the user in effect; no password reaches
  it or `repr()`.
- A config constructor takes a host and refuses a URI (`ValueError` naming
  `HostConfig(uri)`, password redacted). A URI refuses a C0, DEL or C1 control
  character in its host, raw or percent-encoded; a tab, CR or LF in the
  userinfo is kept (percent-encoded before `urlsplit` would delete it), so
  `ssh://u:pw<LF>otp:1@host` authenticates with `pw` plus the `otp` extra.
- An unknown scheme raises `ValueError("unsupported host scheme: ...")`. One
  plugin that fails to import is warned about and skipped; its own scheme then
  raises `RuntimeError` from the import error.

```python
HostConfig.open() -> Host
HostConfig.supported_credentials(uri) -> tuple[str, ...] | None
```

- `config.open()` creates the host; `with config as host:` and
  `with config.open() as host:` connect and always close. Re-entering a config
  whose host is still open raises `RuntimeError`.
- `supported_credentials(uri)` (a classmethod) names the credentials the
  config selected by `uri` accepts, or `None` when it declares no list. Offer
  an ambient credential (one supplied per session, like the command line's
  `HOSTCTL_PASSWORD`) only where it is accepted.

```python
HostConfig._from_parsed_uri(parsed, **credentials) -> HostConfig
HostConfig._create_host() -> Host
```

Implementing a config: `class MyConfig(HostConfig, schemes=("mine",))` with
`uri_credentials = ("password", ...)`, then override the two hooks.
`_from_parsed_uri` (a classmethod) builds the config from a `urlsplit` result,
using `strict_uri_query`, `strict_uri_credentials`, `uri_host` and
`uri_hostname`; `_create_host` returns the host that `config.open()` and
`with config as host` hand out. The single underscore means "override in a
subclass, never call"; there is no `from_uri`. A worked application config
and `PosixHost` subclass is in
<https://jose-pr.github.io/hostctl/guide/extending/>.

- `uri_credentials` makes dispatch reject any other credential before
  construction, so `passwrd=` fails loudly; `None` (the default) skips the
  check for a config that validates its own.
- A scheme another live config already claims raises `ValueError("host scheme
  'x' is already registered by Name")` at class definition; redefining the
  same module-qualified class replaces it, and `replace=True` in the class
  keywords takes the scheme over. Registry state stays private.
- `hostctl.host_schemes()` returns the sorted registered schemes, entry-point
  names included without importing them.

```python
hostctl.host_schemes() -> tuple[str, ...]
Host.run(*cmds, bufsize=-1, executable=None, stdin=None, stdout=None, stderr=None, cwd=None, env=None, capture_output=True, check=True, encoding=None, errors=None, input=None, timeout=..., text=None) -> subprocess.CompletedProcess
Host.path(*segments, backend=None) -> pathapi.Path
Host.spawn(*cmds, executable=None, cwd=None, env=None, terminal=None, encoding=None, errors=None) -> Process
Host.info() -> HostInfo
Host.connect()
Host.close()
Host.name -> str                                   # settable
Host.logger -> logging.Logger | logging.LoggerAdapter  # settable
HostConfig.name -> str                             # settable
```

- `Host` is abstract; `run()`, `path()` and `spawn()` raise
  `NotImplementedError` on a host that cannot do them. A host also exposes
  `scheme`, `connection_uri`, `capabilities`, `executor`,
  `executor_capabilities`, `last_selection` (the provider trace of the most
  recent `run()`, `SelectionStep` records), `shell_flavour` and `shell`, and
  is a context manager.
- `name` is a short label for the target with no credential in it:
  `config.name`, which is the `connection_uri` authority without userinfo
  (`nas.example`, `node:2222`; SSH omits port 22), a QEMU domain, a serial
  device, `localhost` for `local:`. Assigning either replaces it.
- `logger` is where records about the host go: by default a
  `LoggerAdapter` over a `hostctl` logger that prefixes `[name] ` and sets
  `record.host`. Assign a `Logger` or adapter to redirect them.
- A system host logs ONE `WARNING` on `host.logger` per skipped provider when
  `run()` or a path operation is served by a provider ranked below one that
  is unavailable (its probe said so, or it declined before dispatch):
  `run() fell back from ssh to webshell: ssh: <reason>`. Routing an operation
  to the provider that supports it, and a pinned path, are not fallbacks.
  The SSH, SFTP and WinRM providers' reason (also `SelectionStep.reason`)
  says what refused them, with the target and the redacted error: `SSH host
  key of nas:22 is not trusted (...); add the key to known_hosts
  (known_hosts= names the file to read) or pass verify=False ...`, `SSH login
  to nas:22 was refused (...)`, `SSH connection to nas:22 was refused (...)`,
  `SSH host name nas did not resolve (...)`, `SSH connection to nas:22 timed
  out`, `SSH host nas:22 is unreachable (...)`, `WinRM TLS certificate of
  win:5986 is not trusted (...); ...`, else `... failed (Type: message)`.
  The target carries a port only when the config names one: an `SSHConfig`
  without `port=` leaves it to the ssh_config, so the reason says `nas`.
- `run()` returns `subprocess.CompletedProcess`. **`check` and
  `capture_output` default to `True`**, the opposite of `subprocess.run`: a
  failing remote command raises `CalledProcessError`, and its output is
  captured rather than inherited. Pass `check=False` to read `returncode`.
  The command grammar (strings, structured lists, operators, `Exec`) is in
  "Shells".
- Output is `str` when any of `text`, `encoding` or `errors` is given and
  `bytes` otherwise (`subprocess.run`'s rule; `text=False` does not veto an
  `encoding`). A `None` stdout/stderr target means `sys.stdout`/`sys.stderr`;
  `subprocess.DEVNULL` discards.
- `env` is additive on every transport, the local one included: values merge
  over the inherited environment, never replace it. A name outside
  `[A-Za-z_][A-Za-z0-9_]*` raises `ValueError` before any provider is chosen.
- A timeout raises `subprocess.TimeoutExpired` with the same payload on every
  transport: `.orphaned` (`True` when the command is still running on the
  target), `.pid` (`None` unless known), and `.output`/`.stderr` as `b""`/`""`
  rather than `None`.
- `path()` returns a `pathapi.Path`: a composite path on a provider-backed
  host (see "Paths"). Without `backend` each operation selects a provider;
  `backend=name` builds the path with that path provider and pins it there,
  so `host.path(p, backend=name)` is `host.path(p).via(name)`: every
  operation and every derived path uses it. An unknown name raises
  `ValueError`, an unavailable provider `OperationNotStartedError`.
  `spawn()` returns a persistent `Process`; providers advertise `spawn` and
  `tty` separately.
- `info()` returns a `HostInfo` whose unknown fields stay `None`.
- `host.shell_flavour` is the explicitly known target shell, and `host.shell`
  is `Shell(host.shell_flavour, host)`. SSH uses its configured or positively
  detected flavour, WinRM PowerShell, and a local host POSIX `sh` or Windows
  PowerShell by platform.

```python
HostInfo(hostname=None, os_family=None, os_name=None, os_version=None, architecture=None)
```

`ShellSelection` is what every config's `shell=` accepts: a `ShellFlavour`,
its class, its registered name, or `"auto"`. `PathnameSelection` is what
`pathname=` accepts: a pure-path or `pathapi.Pathname` class, `"posix"`,
`"windows"` or `"auto"`; a bare `PurePath` is refused because it would infer
the local OS.

## Timeouts (`hostctl.host`)

```python
Timeout(total=DEFAULT, *, connect=DEFAULT, read=DEFAULT, write=DEFAULT)
Timeout.coerce(value) -> Timeout
DEFAULT
```

- `Timeout` is a frozen, hashable dataclass of four fields, in seconds:
  `total` bounds a whole command (the default of `run(timeout=)`), `connect`
  establishing a connection, session or port, `read` one reply or one read,
  `write` one write. `Timeout(30)` sets `total` only; the other fields are
  keyword-only.
- Each field is `DEFAULT` (the transport's own documented default), `None`
  (no limit) or a positive, finite number, stored as `float`; `read` and
  `write` may also be `0`, "do not wait": return at once with what is there.
  Anything else raises at construction: `TypeError` for a value that is not
  a number, `None` or `DEFAULT` (a `bool` included), `ValueError` for a
  negative number, infinity, or zero as `total` or `connect`. A config whose
  transport cannot return at once refuses a `0` with `ValueError` naming the
  field (`QEMUConfig: timeout.read=0 (do not wait) is not supported`); only
  serial honours it.
- `repr()` shows only the fields that are not `DEFAULT` (`Timeout(total=30.0,
  read=None)`, `Timeout()`) and evaluates back to an equal value.
  `dataclasses.replace()` validates the new value.
- `Timeout.coerce(value)` is what a config's `timeout=` does with its
  argument: a `Timeout` unchanged, a number or `None` as
  `Timeout(total=value)`, `DEFAULT` as `Timeout()`; anything else raises
  `TypeError`.
- `DEFAULT` is a singleton compared with `is`; its `repr` is `DEFAULT`, and
  it pickles and copies to itself.

```python
HostConfig.timeouts -> Timeout
```

- **Every config takes `timeout=`**, keyword-only: a number or `None` (which
  sets `total` only, `SSHConfig("nas", timeout=60).timeout ==
  Timeout(total=60)`), or a `Timeout`. The config stores the value as a
  `Timeout` (`Timeout.coerce`), on construction, on assignment and through
  `dataclasses.replace()`, so `config.timeout` is always a `Timeout` and
  equal spellings make equal configs.
- A field the transport cannot honour raises `ValueError("SSHConfig:
  timeout.write is not supported")` when set to anything but `DEFAULT`,
  `None` included: nothing is silently ignored.
- `config.timeouts` is the resolved value: each supported `DEFAULT` replaced
  by what the transport applies. An unsupported field stays `DEFAULT`, and so
  does a system config's `total`, which the selected provider decides; a
  config without a `timeout` attribute (a third-party `HostConfig`) reports
  `Timeout()`.

| config | `total` (`run()` default) | `connect` | `read` | `write` |
| --- | --- | --- | --- | --- |
| `LocalConfig` | none | -- | -- | -- |
| `SSHConfig` | none | 30 s: TCP, key exchange and authentication (asyncssh `connect_timeout`; `None` leaves asyncssh's own) | -- | -- |
| `WinRMConfig` | 30 s, every leg | 30 s: native `-OpenTimeout`, PSRP `connection_timeout`, pywinrm the connect half of each request (`None` leaves each library's own) | -- | -- |
| `ContainerConfig` | -- | 60 s: the Docker SDK client's request timeout, which bounds connecting to the Engine and each API reply | -- | -- |
| `QEMUConfig` | none | the `read` value: opening the agent stream and its guest-sync (unix and ssh transports only) | 10 s: one guest-agent reply | -- |
| `SerialConfig` | none | -- | 0.1 s: one pyserial read; `0` does not wait | 10 s: one write; `0` does not wait |
| `PosixConfig`, `WindowsConfig`, `IOSConfig` | the selected provider's own | -- | -- | -- |

"--" is unsupported. "none" is no limit. A container has no `total`: an
Engine exec has no cancel, so `run(timeout=<n>)` raises
`NotImplementedError`. QEMU's `connect` is refused with `transport='libvirt'`
(`virConnectOpen` takes no timeout) and with `transport_factory`; a
`qga+ssh` tunnel's SSH connection is bounded by its `ssh=` config's own
`timeout.connect`. Serial's `inter_byte_timeout` stays a separate field: it
is pyserial's gap between two bytes of one read, a framing setting rather
than a bound on an operation. A provider-composed system config's
connection timeouts belong to the transport configs in its
`provider_options`.

- **URI keys**, on every scheme: `timeout` (`total`), `timeout_connect`,
  `timeout_read`, `timeout_write`, in seconds. An empty value is `None`; an
  absent key is `DEFAULT`. `connection_uri` renders only the fields that are
  not `DEFAULT`, so a config round-trips through `HostConfig(str(config))`.
  An unsupported key raises as the keyword does; a removed key raises
  `ValueError("unknown connection parameter: connect_timeout; use
  timeout_connect")` (also `agent_timeout` -> `timeout_read`, serial
  `read_timeout`/`write_timeout` -> `timeout_read`/`timeout_write`).
- **`run(timeout=)`** on every host: seconds for this call, `None` for no
  limit. Left out (shown `timeout=...` in the signatures below), it is the
  config's `timeout.total`; when that is `DEFAULT`, the transport's own
  default (the table). `host.shell.run(timeout=None)` passes `None` on.
  `Process.wait(timeout=)` is unchanged: `None` waits forever.

## Verification (`verify=`)

`WinRMConfig`, `ContainerConfig` and `SSHConfig` (and so a `qga+ssh` tunnel's
`ssh=`) take `verify=`, keyword-only, default `True`:

- `True` checks the peer with the transport's own trust (the table).
- `False` checks nothing: no certificate, host name or revocation check over
  TLS, no host-key check over SSH. It is the only value that switches a check
  off.
- A path (`str` or `os.PathLike`, stored as `str`; a PEM file or an OpenSSL
  hashed directory) is the CA bundle trusted *instead of* the default -- TLS
  transports only. Neither certifi nor `REQUESTS_CA_BUNDLE` replaces it. A
  missing path raises `FileNotFoundError` naming it when the leg is built,
  before connecting.
- Anything else raises: `TypeError` for another type (`None` and `0`
  included), `ValueError` for an empty string or one spelling a boolean
  (`"false"`; write `./false` for such a file). On SSH a string is a
  `TypeError`: SSH has no CA bundle, so name the keys with `known_hosts=`.

| config / leg | `True` trusts | a CA bundle |
| --- | --- | --- |
| `WinRMConfig`, native | the Windows certificate store (PowerShell remoting) | `ValueError` |
| `WinRMConfig`, pywinrm | the OS trust, checked by the platform's verifier (Python 3.10+) or OpenSSL's store (3.9); `REQUESTS_CA_BUNDLE` or `CURL_CA_BUNDLE` instead when set; never certifi | pywinrm `ca_trust_path` |
| `WinRMConfig`, PSRP | the same as pywinrm | pypsrp `cert_validation` |
| `ContainerConfig`, `https://` `engine_url` | the same as pywinrm | docker-py `TLSConfig(verify=)` |
| `SSHConfig` | `known_hosts` (see SSH) | -- (`TypeError`) |

- **The OS trust**, on Python 3.10+ with `truststore` (which the `winrm`,
  `psrp` and `container` extras install there), is decided by the
  platform's own verifier. Windows: CryptoAPI, as for Schannel -- a public
  root the machine has not stored yet is fetched from Windows Update.
  macOS: the Security framework, keychains included. Linux and the BSDs:
  OpenSSL with the system's CA file and directory (`update-ca-trust`,
  `update-ca-certificates`, `certctl`; `SSL_CERT_FILE`/`SSL_CERT_DIR`
  override them). Only the sessions hostctl builds use it; `ssl` is not
  patched for the rest of the process.
- **On Python 3.9, or without `truststore`**, it is what
  `ssl.create_default_context()` loads. Windows: the ROOT and CA stores as
  they are on the machine -- a root Windows downloads on first use is absent
  until something has fetched it. Linux and the BSDs: as above. macOS: the
  Python build's OpenSSL file (`/etc/ssl/cert.pem` for Apple's Python), not
  the keychain -- a CA added to a keychain needs `verify=` or
  `REQUESTS_CA_BUNDLE`.
- A set CA variable is the only trust, as it is in `requests`, checked by
  OpenSSL; a missing file it names raises `FileNotFoundError` naming the
  variable. The `winrm`, `psrp` and `container` extras need
  `requests>=2.32.3`, `urllib3>=2.0.2` (1.26 refuses `verify=True` to a host
  given by IP address) and, on Python 3.10+, `truststore>=0.10.5`.

- **URI key** `verify`: `1/true/yes/on` or `0/false/no/off`, else (TLS
  schemes) a percent-encoded bundle path. `connection_uri` renders it where
  it is not the default (WinRM always renders it), so a config round-trips.
- A WinRM bundle needs `tls=True`, and `provider="native"` refuses one at
  construction (`provider="auto"` choosing native refuses it when the leg is
  built). A container's non-default `verify` needs an `https://` `engine_url`
  and no `client_factory`, as does its `client_cert` (see Containers).

## URIs (`hostctl.uri`)

```python
ConnectionString(value, *, scheme=None, host=None, port=None, username=None, password=None, extras=None, path=None, query=None, fragment=None, defaults=None)
ConnectionString.replace(**changes) -> ConnectionString
ConnectionString.geturl() -> str
ConnectionString.query_val(name, default=None)
```

The parsed form of a target -- scheme, host, port, user, password, extras --
so a caller never reimplements connection-string parsing. `is_local` is
answered by netimps rather than guessed; `authority` and `qsl` are derived
properties; `replace(**changes)` returns a copy; `query_val` returns the last
value for a query key. A bare host (`nas`) or `host:port` (`nas:8443`) needs
`scheme=` or `defaults={"scheme": ...}` and raises "no scheme" without one. An
opaque URI with no host (`local:`, `serial:COM3`) parses, with an empty host
that `is_local` calls local.

Raises `ValueError` for anything that is not a valid URI: an unclosed IPv6
literal, a non-numeric port, or an unescaped `/`, `?` or `#` in a password.
**Percent-encode reserved characters in a password**: there is no lenient
mode, because a password may contain `@` and `:` and nothing can recover where
an unescaped one ended (`@` and `:` do parse: last `@`, first `:`, per the URI
grammar). The error for an unencoded `/`, `?` or `#` in a password (from
`HostConfig(uri)`, `Host(uri)` and `ConnectionString`) is a fixed message that
never echoes the URI or any part of the password. Only an authority that does
not parse gets it: a URI whose port is valid is taken at face value, so
`ssh://nas:22/mail/user@example.com` and `ssh://root:123/rest@nas` name host
`nas`/`root` with an `@` in the path, as `redact_uri` reads them too.

```python
redact_uri(uri) -> str
parse_credentials(password) -> tuple[str, dict[str, str]]
```

- `redact_uri` strips a password and returns a valid, reusable URI (not a
  masked one, so a rendered form never round-trips a wrong credential), for
  logs, reprs and error messages. It never raises. A well-formed URI is never
  over-redacted: when it parses, `urlsplit`'s authority is the authority, so
  an `@` elsewhere (`ssh://nas:22/mail/user@example.com`) is left alone. Only
  input that does not parse gets a textual guess at the last `@`, which may
  over-redact. Use it when the text may not parse at all and the job is only
  keeping a secret out of a log.
- `parse_credentials` splits a password field on newlines: the first line is
  the password, each later line a credential extra; `name:value` sets a value
  and a bare `name` is `name:` (empty string). Names are casefolded and
  stripped, values keep everything after the first `:`, blank lines are
  ignored, and CRLF is handled.

```python
strict_uri_query(parsed, allowed, *, repeatable=()) -> dict[str, object]
strict_uri_credentials(credentials, allowed)
uri_host(host) -> str
uri_hostname(parsed: SplitResult) -> str
```

The helpers a config's `_from_parsed_uri` uses.

- `strict_uri_query` returns the query of a `urlsplit` result as a dict and
  raises `ValueError` for a key outside `allowed`, or a repeated key unless it
  is in `repeatable` (whose values arrive as an ordered list).
- `strict_uri_credentials` raises `ValueError` naming any credential outside
  `allowed`.
- `uri_host(host)` takes a `str` and brackets an IPv6 literal for a URI
  authority.
- `uri_hostname(parsed)` takes a `urlsplit` result (a `str` raises
  `AttributeError`) and returns its host as written, IPv6 unbracketed, `""`
  when there is none. `urlsplit().hostname` is lowercased; this keeps the
  original case, for a value that goes back into a URI or a log. Use it
  wherever `_from_parsed_uri` stores a host.

## Systems (`hostctl.system`)

A system host owns the target's semantics (its shell and path grammar) and
reaches it through ordered providers. Opening an `SSHConfig` or `WinRMConfig`
returns a `PosixHost` or `WindowsHost` composed of that transport's providers;
the transport itself is private.

```python
SystemConfig(authority='localhost', *, shell=None, executor=None, path=None, provider_options=None, initializer=None, timeout=Timeout())
PosixConfig(authority='localhost', *, shell=None, executor=None, path=None, provider_options=None, initializer=None, timeout=Timeout())
WindowsConfig(authority='localhost', *, shell=None, executor=None, path=None, provider_options=None, initializer=None, timeout=Timeout())
IOSConfig(authority='localhost', *, shell=None, executor=None, path=None, provider_options=None, initializer=None, timeout=Timeout())
```

The `posix://`, `windows://` and `ios://` configs compose a system host from
provider descriptors instead of owning a transport. `executor=` and `path=`
are sequences of descriptor names (a bare string raises `TypeError`); the
built-ins are `local`, `ssh`, `sftp` and `winrm`, and a transport descriptor
needs its config in `provider_options` (`{"ssh": SSHConfig(...)}`), which
never serializes credentials into the canonical URI. Left out (`None`), a
kind takes the host class's `default_providers()`; `()` composes none.
`initializer` is a `SessionInitializer` or callable run once per connection
generation.

```python
SystemHost(config=None, *, executor_providers=None, path_providers=None, shell=None, info=None, initializer=None)
PosixHost(config=None, *, executor_providers=None, path_providers=None, shell=None, info=None, initializer=None)
WindowsHost(config=None, *, executor_providers=None, path_providers=None, shell=None, info=None, initializer=None)
IOSHost(config=None, *, executor_providers=None, path_providers=None, shell=None, info=None, initializer=None)
SystemHost.replace_providers(*, executor=None, path=None)
SystemHost.resolve_providers(executor=None, path=None) -> tuple[tuple[ExecutorProvider, ...], tuple[PathProvider, ...]]
SystemHost.default_providers() -> tuple[tuple[str, ...], tuple[str, ...]]
SystemHost.provider_resolvers: ClassVar[Mapping[str, resolver | None]] = {}
SystemHost.runspace()
```

- A system host takes a `HostConfig`, never a connection string, and the
  providers to compose. `config_type` names the config class a host belongs
  to; constructed with no config, a host builds `config_type()` (`PosixHost()`
  a `PosixConfig()`). `IOSHost` is session/command-only: it has no path
  grammar, so `path()` raises.
- `executor_providers` and `path_providers` are the ordered tuples the host
  was composed from; `provider_details` reports availability without
  dispatching. Left out (`None`), a kind is resolved from the config's
  descriptor names (its `executors`/`paths` attributes) by
  `resolve_providers()`.
- `resolve_providers(executor=None, path=None)` builds providers from
  descriptor names in order; `None` takes the config's names, or
  `default_providers()` when the config names none. Each resolver runs once
  per call, so the two legs of one transport share it: pass both sets of one
  call to `replace_providers()`. An unknown name raises `ValueError` listing
  the names this host knows; a descriptor offering no provider of the kind
  raises `ValueError`.
- `default_providers()` returns `(executor_names, path_names)`, empty for the
  built-in hosts; override it per host class (it may read `self.config`). A
  default `ssh`/`sftp` is dropped when `has_ssh()` is false; a name the config
  gives is kept and fails at use with the install hint.
- `provider_resolvers` maps a descriptor name to `resolver(host) ->
  (executor provider or None, path provider or None)`. Merged up the MRO (the
  most derived class wins) and consulted before the global registry; `None`
  hides an inherited or global name. The resolver is handed the host at the
  end of `SystemHost.__init__`, with `config` and the subclass's own
  attributes set.
- `replace_providers()` swaps either set (`None` keeps it), closes each
  replaced transport once unless a kept provider shares it, and starts a new
  connection generation, so the initializer runs again. Duplicate names raise
  before anything changes. Paths obtained earlier keep the old providers.
- `connect()` raises `OperationNotStartedError("no provider connected: ...")`
  when providers were offered and none connected (one is enough).
- `runspace()` returns a provider-owned typed runspace (the WinRM PSRP
  `RunspaceSession`) when one is available.

```python
register_system_provider(name, resolver, *, replace=False)
system_provider_names() -> tuple[str, ...]
```

`register_system_provider` adds a descriptor every system host accepts:
`resolver(host)` returns `(executor provider or None, path provider or None)`
(`ssh_providers(config)`, `winrm_providers(config)` and `local_providers()`
already have that shape). A resolver taking more than the host raises
`TypeError`. A name already registered raises `ValueError` unless
`replace=True`; an unknown descriptor loads the `hostctl.system_providers`
entry point of that name. `system_provider_names()` lists the global names,
entry points included without importing them; a class's
`provider_resolvers` add to and hide from that list.

## Local (`hostctl.transports.local`)

```python
LocalConfig(*, timeout=Timeout())
LocalHost(config=None, *, executor_providers=(), path_providers=(), **options)
local_providers() -> tuple[ExecutorProvider, PathProvider]
LocalExecutor.__call__(command, *args, bufsize=-1, stdin=None, stdout=None, stderr=None, cwd=None, env=None, capture_output=True, check=True, encoding=None, errors=None, input=None, timeout=None, text=None, **options) -> subprocess.CompletedProcess
```

- `LocalConfig` is the `local:` URI. `LocalHost` is a `SystemHost` over the
  local providers; its family and shell come from `os.name`, and `info()`
  reports this process's platform. `**options` are `SystemHost`'s keywords.
- `local_providers()` returns the local `(executor_provider, path_provider)`
  pair for a host you assemble yourself.
- `LocalExecutor` runs through `subprocess.run` with native argv, `cwd`,
  environment (merged over `os.environ`), streams, encoding, `check` and
  `timeout`. On Windows a `.bat`/`.cmd` target of `Exec` is quoted with cmd's
  rules (see `ShellTarget.BATCH`): the program in plain quotes, a `"` or `%`
  in its path raising `ValueError`.
- On Windows, when this process has no console (a GUI app, `pythonw`, a
  service, an editor's background task), the local executor and the native
  WinRM leg start children with `CREATE_NO_WINDOW`, so no console window
  flashes per call. `LocalExecutor()(..., creationflags=...)` replaces that
  with the caller's own flags (e.g. `subprocess.CREATE_NEW_CONSOLE`).

## SSH (`hostctl.transports.ssh`)

```python
SSHConfig(host, *, port=None, username=None, default_username=None, password=None, client_keys=None, executable=None, verify=True, known_hosts=(), shell=POSIX_SHELL, pathname=PosixPathname, login_shell='auto', timeout=Timeout(), ssh_config=USER_SSH_CONFIG, connect_options={})
ssh_providers(config) -> tuple[ExecutorProvider, PathProvider]
USER_SSH_CONFIG
```

`ssh://user@host:port`; needs the `ssh` extra. Opening it returns a
`PosixHost` (or `WindowsHost` for a Windows path flavour) whose `run()` uses
an AsyncSSH exec channel and whose paths use an `sftppath.SFTPPath` backend.

- `username=None` leaves the account to the ssh_config's `User`, else the
  local user, and `port=None` the port to its `Port`, else 22, as `ssh` does
  without `-l`/`-p`; a value you pass wins over the config. `ssh://h` has no
  port (`port is None`); `ssh://h:22` names 22.
- `default_username` replaces the local user in that chain: `username` (the
  URI's user) > the ssh_config's `User` > `default_username` > the local
  user. For a library whose targets have a known login (`"root"`) that a
  user's `~/.ssh/config` may still override. It is read as the `User` of a
  configuration file asyncssh reads after `ssh_config` (any `User` there,
  `Host *` included, wins), so a `Match user` in the ssh_config sees the
  local user. Not part of the URI. Refused, with `ValueError`, where a
  `username` would be and also for control characters and `"none"`.
  `client_keys` is an `SSHClientKeys` (one `SSHKeySource` -- a key
  path, bytes or text -- or a sequence of them); `known_hosts` an
  `SSHKnownHosts`, the same shape but never `None`. Authentication fields are
  excluded from `repr()`.
- `password` is a string or a callable `() -> str | None`; asyncssh calls the
  callable only when the server asks for a password or a keyboard-interactive
  answer, after key authentication failed -- never when a key is accepted --
  once per connection attempt (the exec and SFTP legs may each attempt).
  Cache the answer in the callable to ask once. An exception it raises is
  the root `cause` of the `OperationNotStartedError` that reports the
  refused connection.
- Host keys: `known_hosts=()` (the default) is the lookup `ssh` does: the
  ssh_config's `UserKnownHostsFile`, else `~/.ssh/known_hosts`; anything
  else names the trusted keys. `verify=False` (`?verify=false`) replaces the
  usual lookup with no check; a `known_hosts` you name is checked even then
  (the stricter setting wins). `known_hosts=None` raises `ValueError` naming
  `verify=False`. The ssh_config is honoured as written, so its
  `UserKnownHostsFile none` switches the check off.
- `host` and `username` are refused where OpenSSH would refuse them (a
  leading `-`, whitespace, shell metacharacters, `${` in a username); the host
  must be an address or a valid name. `ssh://h:0` is a `ValueError`, not port
  22.
- `shell` selects command construction independently of the SFTP path
  flavour. `shell="auto"` probes positively, caches, and raises rather than
  guessing; it accepts sh, dash, ash, ksh, ksh93, mksh, lksh, oksh, yash,
  posh, bash, zsh and fish (csh and tcsh are refused) and keeps the
  executable path the probe reported.
- `pathname="auto"` connects while the host is created and reads the SFTP
  login directory: `/C:/...` is Windows, any other `/`-rooted answer POSIX,
  anything else raises.
- `login_shell` names what parses the command string when it arrives: on
  POSIX `$SHELL -c`, which is what hostctl renders for, but Windows OpenSSH's
  stock server hands it to `cmd.exe /c`. `"auto"` reads the dialect (a Windows
  path flavour means `cmd.exe`); name it (`"posix"`, `"cmd"`, `"powershell"`)
  when the server's `DefaultShell` has been changed.
- `timeout` (see "Timeouts"): `total` is the default of `run(timeout=)`,
  none unless set; `connect` bounds establishing the connection (TCP, key
  exchange, authentication), 30 s, and `None` there keeps asyncssh's own
  (an ssh_config `ConnectTimeout`, else the OS connect timeout plus
  asyncssh's 2-minute login timeout). `read`/`write` raise.
- `ssh_config` is the OpenSSH client configuration of the host's one
  connection (`Host`, `HostName`, `User`, `Port`, `IdentityFile`,
  `ProxyJump`, `ProxyCommand`, `UserKnownHostsFile`, `Include` ...):
  - `USER_SSH_CONFIG` (the default; exported from `hostctl` and
    `hostctl.transports.ssh`, one object, `repr()` `USER_SSH_CONFIG`) is
    `~/.ssh/config`, `~` resolved each time a connection opens; a missing
    file reads nothing and fails nothing, as with `ssh`.
  - `None` or `False` reads none.
  - An `os.PathLike` (`pathlib.Path`, a local `pathapi.Path`) names a file,
    `~` expanded when the connection opens.
  - **A `str` is the configuration's text, not a file name**: name a file
    as `pathlib.Path(...)`. A line holding a keyword and no value (what a
    file name is when read as text) raises `ValueError`; anything else is
    not second-guessed.
  - `bytes`, `bytearray`, `memoryview` and a stream (`read()`, e.g.
    `io.StringIO`/`io.BytesIO`) are the text too, read when the config is
    built; bytes are decoded as UTF-8 (`ValueError` otherwise).
  - Any other object with `read_text()` or `open()` (a `pathapi.Path` on
    another host, a `MemPath`) is read as UTF-8 text through that object
    when the first connection opens, and the text is kept for later ones
    (and for a `dataclasses.replace()` copy).
  - Other values raise `TypeError`; `True` raises `ValueError`.

  Text reaches asyncssh (which reads configurations only from files)
  through a private temporary file, created readable by this user only,
  written in the encoding `open()` reads it with, removed as soon as the
  connection is open or has failed (and at interpreter exit as a fallback).
  It is never logged, never in `repr()` (`ssh_config=<ssh_config text>`) or
  `connection_uri`. A relative `Include` in it resolves against `~/.ssh`.
  `ssh_config` is never a URI key, and an `ssh://` URI naming it raises: a
  `ProxyCommand` starts a local process, which a URI's author must not
  choose.
- `connect_options` are extra `asyncssh.connect` keywords for that
  connection, kept out of the URI and `repr()`; a key hostctl owns (`host`,
  `port`, `username`, `password`, `client_keys`, `known_hosts`, `config`,
  `connect_timeout`) raises and names the keyword to use
  (`timeout=Timeout(connect=...)` for `connect_timeout`).
- `ssh_providers(config)` returns the `(executor_provider, path_provider)`
  pair sharing one transport, for a host you assemble yourself; building them
  separately would open two connections and close one.

```python
SSHExecutorProvider.connection -> asyncssh.SSHClientConnection
SSHExecutorProvider.loop -> asyncio.AbstractEventLoop
```

`SSHExecutorProvider` is the executor provider `ssh_providers(config)[0]`
returns and a composed SSH host holds in `executor_providers` (name `"ssh"`);
find it with `isinstance`. Its constructor takes the private transport, so
obtain it only through those two routes.

- `connection` is the asyncssh connection `run()` and `spawn()` use, and
  the one the host's SFTP paths open their channels on, for port forwarding
  or an SFTP client of your own. It connects on first access
  (raising what connecting raises: `PermissionError`, `ConnectionError`,
  `TimeoutError`, or the `OSError` of a refused or unresolvable host --
  never `OperationNotStartedError`) and returns the same object until it
  closes. A first access from a coroutine running on `loop` raises
  `RuntimeError`.
- It is bound to `loop`, the process-wide `hostctl-asyncssh-loop` thread's
  event loop (see "Gotchas"), never the caller's. Run a coroutine of it with
  `asyncio.run_coroutine_threadsafe(coro, provider.loop).result()`; awaiting
  one on another loop is unsupported. Do not block that loop: every SSH host
  in the process runs on it.
- `host.close()` (or `provider.close()`) closes the connection and waits for
  it. Reading `connection` afterwards opens a new one outside the host's
  lifecycle; `provider.close()` closes that one. After `fork()` the child
  gets a new connection on a new loop, so read both again there.

Behaviour:

- A composed SSH host opens one connection and authenticates once, on its
  first command, relative path or path operation, whichever comes first.
  Paths open SFTP channels on that connection (sftppath's
  `AsyncSSHBackend.over_connection()`), so `ssh_config`, `connect_options`
  and `timeout.connect` apply once, to it. Building an absolute path connects
  nothing. After `close()` or a dropped connection the next command or path
  operation opens a new connection, and a path taken before then runs on it.
  A failed connect on the path leg declines the SFTP provider
  (`OperationNotStartedError`), as on `run()`. An SSH path's
  `SFTPPath.backend` cannot be pickled (`TypeError`), and its `close()`
  exits the SFTP channel only.
- A relative path (and `host.path()`) resolves against the login directory,
  read once per connection generation with SFTP `realpath(".")`; `str()` of
  the result is the resolved path on the server. When the exec connection
  cannot be opened (refused, unresolvable, unreachable, rejected), the SFTP
  provider declines with `OperationNotStartedError` and the host builds the
  path from its next path provider.
- Authentication failures raise `PermissionError`; host-key, key-exchange,
  disconnect, connection-loss, protocol and channel failures raise
  `ConnectionError`, with the AsyncSSH exception as `__cause__`. A refused
  exec request is a plain `OSError`. The SFTP provider probes `unavailable`
  (naming `hostctl[ssh]`) when sftppath is missing, so selection routes around
  it.
- `run()` sends an explicit EOF for omitted stdin, buffers the whole response
  in memory even when `stdout=` names a sink, reads a file-object `stdin=`
  fully before dispatch, and reports a missing exit status as `-1`. `bufsize`
  is asyncssh's stdin chunk size, so values below 512 (and `0`) are refused.
- A timeout sends TERM, waits up to 2 s for the channel to close, then closes
  it and waits 1 s more, so the call returns within about three seconds of
  the deadline; `orphaned` is false only when the channel closed during the
  TERM grace. A timed-out `wait()` of a spawned process keeps what it drained
  for `read()`, and the exception carries a copy.
- PowerShell over SSH: `PowerShellFlavour.command(..., login_shell=,
  stdin_free=)` renders `-Command SCRIPT` for a `cmd` login shell (a one-line
  script, escaped for the extra parse) and a POSIX one; a script with a `#`
  goes on the channel's stdin (`-Command -`) when the call has no
  `input=`/`stdin=`; a multi-line script or taken stdin falls back to
  `-EncodedCommand` (CLIXML stderr, decoded for `run()`; endpoint protection
  flags it). Every form starts with `$ProgressPreference='SilentlyContinue'`,
  and `PWSH` adds `$PSStyle.OutputRendering = 'PlainText'`, so captured output
  carries no ANSI colour.
- See "Gotchas" for fork behaviour and the event-loop thread.

## WinRM (`hostctl.transports.winrm`)

```python
WinRMConfig(host, *, username, password=None, auth='ntlm', port=None, tls=False, verify=True, message_encryption='auto', timeout=Timeout(), provider='auto', cleartext_auth=False)
winrm_providers(config) -> tuple[ExecutorProvider, PathProvider]
```

`winrm://` (HTTP) and `winrms://` (HTTPS); needs the `winrm` extra, `psrp` for
runspaces. Opening it returns a `WindowsHost` running PowerShell, whose paths
are a `CompositeWindowsPath` over the WinRM path backend.

- `auth` is the scheme (`ntlm`, `kerberos`, `credssp`, `basic`, `plaintext`,
  `ssl`; CredSSP and Kerberos need `winrm-credssp`/`winrm-kerberos`).
  `basic`, `plaintext` or `ssl` over plain HTTP sends the password in
  cleartext and raises `ValueError` unless `cleartext_auth=True` (URI
  `?cleartext_auth=true`).
- `tls=True` is HTTPS. `verify` (see "Verification") is `True`, `False`
  (`?verify=false`, no certificate checks) or a CA bundle path for the
  pywinrm and PSRP legs. `str(config)` omits an unset port.
- `timeout` (see "Timeouts") honours two fields. `total`, 30 s, is the
  default of `run(timeout=)`: the same total deadline per command on every
  leg, which a per-call `timeout=` overrides; `None` sets no default.
  `connect`, 30 s, bounds establishing the connection, as on `SSHConfig`:
  native `New-PSSessionOption -OpenTimeout`, PSRP pypsrp's
  `connection_timeout`, pywinrm the connect half of every HTTP request's
  `(connect, read)` timeout. `None` there keeps each library's default:
  PowerShell's 180 s, pypsrp's 30 s, and on pywinrm the 30 s read limit
  bounds connecting too. `read`/`write` raise.
- Per-request timing is fixed: the server holds a silent Receive up to 20 s
  (WS-Man `OperationTimeout`) and one HTTP read may take 30 s.
  `read_timeout=`/`operation_timeout=`/`connect_timeout=` raise `TypeError`;
  the first two as URI keys (and 0.3's `*_timeout_sec`) raise `ValueError`
  naming `timeout_connect` and `timeout`.
- `provider` is `auto`, `native`, `pywinrm` or `psrp`. `auto` picks a
  password-free config on a Windows client as `native` (current-context
  PowerShell remoting, see "Environment variables"); otherwise PSRP when
  `hostctl[psrp]` is installed on Python 3.10+, otherwise pywinrm. `native`
  takes no password string; `pywinrm`/`psrp` need one (or `auth="kerberos"`);
  `provider="psrp"` without the extra raises the install hint.
- `password` may be a callable `() -> str | None`, called only when the leg
  needs a password, as it builds its session and before anything is sent:
  `auto` with a callable takes `native` without calling it when native can
  serve the config (a Windows client, `username` the current logon,
  `ntlm`/`kerberos`, no CA bundle), and otherwise PSRP or pywinrm, which call
  it; Kerberos never calls it. `None` from it raises `PermissionError`.
- `winrm_providers(config)` returns the provider pair sharing one transport.

Behaviour:

- Every leg reports one status: 0 when the last statement succeeded, else the
  native exit code, else 1; a terminating error that sends no status is 1.
  Remote error records are a non-zero status with the text on stderr; only
  transport and authentication failures raise (`ConnectionError`,
  `PermissionError`). A remote ACL denial is a status, not `PermissionError`.
- The pywinrm leg runs `powershell.exe -NoProfile -NonInteractive -Command -`
  without cmd.exe and sends the script on stdin, so scripts have no
  command-line cap; every leg shares one 256,000-byte script budget. WinRM
  stdin is unsupported.
- A deadline raises `subprocess.TimeoutExpired` with `orphaned=True`.
  native: the local `powershell.exe` relay is killed and the remote command
  keeps running. pywinrm: the deadline is checked between WinRS Receives, so
  it is noticed up to 20 s (one silent Receive) late; the command is sent
  the WinRS terminate signal and its shell deleted. PSRP: each Receive is
  shortened to the time left, then the pipeline is sent PSRP's stop signal.
  Termination is requested, never confirmed.
- An expired `timeout.connect` is not a deadline: it raises
  `ConnectionError`. On PSRP the runspace opens before dispatch, so that is
  `OperationNotStartedError`; pywinrm connects per request, so it may come
  mid-command, with the command's effect unknown.
- `timeout=None` (on the config or the call) waits for the command however
  long it takes, and on the native leg a host that stops answering after the
  session opened can block the call indefinitely.
- On the native leg `verify=False` becomes `New-PSSessionOption -SkipCACheck
  -SkipCNCheck -SkipRevocationCheck`, and the remote exit code is carried
  back explicitly.
- PSRP `run()` text is rendered by `Out-String -Stream` in the runspace
  (tables as on the other legs, CRLF-joined, 120 columns).
- A WinRM path's `open("rb")` fetches bounded ranges; writable modes stage
  content and transfer Base64 chunks on close (see "Paths").
- A WinRM path supports pathapi's `Utime`: `_utime()` sets the UTC last-write
  and last-access times to NTFS's 100 ns tick (truncated), on a read-only
  file too; `stat()` reports whole seconds.

```python
RunspaceSession(config=None, *, pool=None)
RunspaceSession.invoke(script, *args, raw=False, capture_exit=False, format_text=False, timeout=None) -> PipelineResult
RunspaceSession.connect()
RunspaceSession.close()
PipelineResult(output, streams, state, had_errors, returncode)
PipelineStreams(error=(), warning=(), verbose=(), debug=(), information=(), progress=())
```

A PSRP runspace with typed PowerShell streams and state, reached through
`host.runspace()`; it is not a byte-oriented `spawn`/TTY process.
`invoke(script, *args)` renders its arguments with the PowerShell flavour's
`structured_command` and returns typed objects in `PipelineResult.output`.
`invoke(timeout=)` is a total deadline in seconds; it has no default (the
config's `timeout.total` applies to `run()`), and on expiry the pipeline is sent
the stop signal and `TimeoutExpired` (`orphaned=True`) is raised. An
injected `pool` is caller-owned: `close()` leaves it open and the session stays
reopenable.

## Containers (`hostctl.transports.container`)

```python
ContainerConfig(container, *, engine_url=None, verify=True, username=None, workdir=None, executable=None, shell='auto', pathname='auto', timeout=Timeout(), client_factory=None, client_cert=None)
ContainerHost(config, *, executor_providers=(), path_providers=())
```

A Docker Engine container; needs the `container` extra. Inspection selects
Linux/POSIX or Windows/PowerShell semantics. `ContainerHost` supports
buffered exec, persistent sessions and TTYs, and archive-backed POSIX or
Windows paths. `client_factory` builds the Docker client (default:
`docker.DockerClient` for an `engine_url`, otherwise `docker.from_env`).

- `verify` (see "Verification") governs an `https://` `engine_url` only: a
  CA bundle or `False` there is honoured on every request, a
  `REQUESTS_CA_BUNDLE` notwithstanding. With another engine URL, no
  `engine_url` (`docker.from_env` reads `DOCKER_TLS_VERIFY` and
  `DOCKER_CERT_PATH`) or a `client_factory`, anything but `True` raises
  `ValueError`. Under `False` urllib3 warns `InsecureRequestWarning` per
  request.
- `client_cert=(certificate, key)` (`str` or `os.PathLike` PEM paths, stored
  as a tuple of `str`) is presented to an `https://` `engine_url`, as a
  daemon run with `--tlsverify` requires; one PEM holding both is
  `(pem, pem)`. It reaches docker-py as `TLSConfig(client_cert=...,
  verify=...)` with `verify` resolved as above, and a `True` there keeps
  docker-py's default trust. Never in `repr()` or `connection_uri`; beside
  a URI it is a credential: `HostConfig("docker://app?engine_url=...",
  client_cert=(c, k))`. Anything but a two-item pair raises `TypeError`
  (an empty path `ValueError`); another engine URL, no `engine_url`, or a
  `client_factory` raise `ValueError`. A missing file raises
  `FileNotFoundError` naming it when the client is built, before any
  request.

- `run()` takes no stdin and no `timeout=`: Docker's buffered exec streams
  neither, and the exec API has no cancellable deadline.
- Archive paths support stat, traversal, read, write, append,
  exclusive-create, `symlink_to()` and `readlink()`; `mkdir`, remove, rename
  and `chmod` raise `NotImplementedError`. `iterdir()`/`walk()` pull one
  archive per directory, so list a large tree through `run()`.
- `_utime()` (pathapi's `Utime`) is not an archive call: it execs
  `touch -c -d @SECONDS.NANOSECONDS` as uid 0 in a Linux container (whole
  seconds on busybox) and the WinRM PowerShell script through `powershell.exe`
  in a Windows one. One probe per path backend decides
  `supports(path, Utime)`; a container without the program, or a client
  without `exec_run`, answers False and `_utime()` raises
  `NotImplementedError`.
- An overwrite keeps the existing file's owner and mode; a new file is uid/gid
  0, mode 0644, and `username=` applies to exec only. `exists()` and
  `open("x")` read one member header. A path taken before
  `ContainerHost.close()` refuses (`ValueError`) instead of reopening the
  client.

## QEMU guest agent (`hostctl.transports.qemu`)

```python
QEMUConfig(domain, *, transport='libvirt', connection=None, socket_path=None, ssh=None, timeout=Timeout(), max_reply_size=48*1024*1024, shell='auto', pathname='auto', transport_factory=None, serial_console=None, path_helper=None)
QEMUHost(config)
QEMUHost.open_serial(*, encoding=None, errors=None) -> Process
```

A guest reached through the QEMU guest agent (QGA). `transport` is a
`QEMUTransport`: `"libvirt"` (local libvirt, the `qemu-libvirt` extra),
`"unix"` (a socket at `socket_path`) or `"ssh"` (a remote Unix socket over an
AsyncSSH tunnel described by `ssh=SSHConfig(...)`, whose `verify` and
`known_hosts` govern the host key; `qga+ssh://...?verify=false` sets it, and
the other schemes refuse the key). `transport_factory`
returns a `GuestAgentTransport` instead. Discovery positively probes QGA and
its enabled command list.

- `run()` uses buffered `guest-exec`/`guest-exec-status` and declares argv
  only: `guest-exec`'s environment replaces the guest's, so the host embeds
  assignments in the rendered script and a direct `Exec` refuses
  `cwd=`/`env=`. QGA cannot cancel a timed-out process: `orphaned` is true and
  `.pid` is kept. `run(stderr=STDOUT)` interleaves the streams on qemu-ga 8.0+
  with a non-Windows guest and otherwise returns stdout then stderr. Results
  carry `stdout_truncated`/`stderr_truncated` and warn when set.
- `max_reply_size` bounds one QGA reply. `timeout.read` (10 s) bounds one
  guest-agent round trip independently of a command's `timeout=`: an agent
  that stops answering raises `ConnectionError`, not a command timeout;
  `None` waits for each reply without a limit (libvirt's
  `VIR_DOMAIN_QEMU_AGENT_COMMAND_BLOCK`). `timeout.connect` bounds opening a
  unix or ssh agent stream and its guest-sync; left `DEFAULT` it shares the
  first request's reply deadline. `timeout.total` (none) is the default of
  `run(timeout=)`. `timeout.write` raises.
- `shell="auto"`/`pathname="auto"` need positive evidence of the guest family
  (`guest-get-osinfo`, or a family-exclusive command in `guest-info`) and
  raise rather than default to POSIX. `info().os_family` comes from
  `kernel-name`.
- Paths use bounded file-handle RPCs; `exists()` needs nothing else, while
  metadata and namespace operations need a guest path helper and raise
  `NotImplementedError` without one. `write_bytes()` and `open("w")` write in
  place (the file keeps its inode, mode, owner and links); only the exclusive
  `x` modes need the helper. `rename()` follows `os.rename` for the guest
  family: it replaces on POSIX and raises `FileExistsError` on Windows. A
  listing skips any name that is not one path component; `open("rb")` is
  seekable where `guest-file-seek` is advertised. A path handed out before
  `QEMUHost.close()` refuses instead of reconnecting.
- `_utime()` (pathapi's `Utime`) needs the helper too, and
  `supports(path, Utime)` is False without one. Times are kept to the
  nanosecond on a GNU guest, whole seconds on busybox, 100 ns on Windows;
  `stat()` reports whole seconds.
- A guest-agent error reply raises `QGACommandError`, which path operations
  translate into the matching `OSError`; a malformed or oversized reply
  raises `QGAProtocolError` (see "Exceptions").
- An injected `serial_console` adds the `serial` capability and
  `open_serial()`: a raw, exclusive console with no shell or status claim;
  `encoding=` makes it read and write `str`. To run commands on a guest
  shell there, drive the byte-mode process with a console profile:
  `PosixConsoleProfile().execute(console_stream(guest.open_serial()), ...)`.

```python
GuestPathHelper.stat(path, *, follow_symlinks=True)
GuestPathHelper.scandir(path)
GuestPathHelper.mkdir(path, mode)
GuestPathHelper.unlink(path, *, missing_ok=False)
GuestPathHelper.rmdir(path)
GuestPathHelper.rename(path, target, *, replace=False)
GuestPathHelper.chmod(path, mode, *, follow_symlinks=True)
GuestPathHelper.utime(path, *, atime=None, mtime=None)
PosixGuestPathHelper(run)
PosixGuestPathHelper.probe(run)
WindowsGuestPathHelper.probe(run)
```

`GuestPathHelper` is the protocol `QEMUConfig(path_helper=...)` takes; without
one, discovery probes a built-in. `run(program, *args, **options)` returns a
`subprocess.CompletedProcess` (a QEMU host's `executor` is one). `probe(run)`
returns a helper, or `None` when the guest cannot support it, and the guest
keeps its read/write-only paths. `utime` is optional on a supplied helper:
without it the paths do not support `Utime`.

- `PosixGuestPathHelper` drives GNU `stat -c` and `find -printf` (proven by
  `stat -c %f -- /` answering hex). Every path is an argv element after `--`,
  so a leading `-` is data and nothing is interpolated into shell text;
  `find` takes no `--`, so it gets an absolute path or one anchored with `./`.
  Of a regular file, `scandir` lists nothing. `utime` runs `stat -L`, then
  `touch -c -d @SECONDS.NANOSECONDS` (`-a`/`-m` for one time); a busybox
  `invalid date` for the fraction switches it to whole seconds, floored.
- `WindowsGuestPathHelper` sends the WinRM path backend's PowerShell scripts
  over `guest-exec` as `powershell.exe -Command -` with the script as input,
  so no script is on the command line; it refuses what Windows cannot express.

```python
GuestAgentTransport.execute(request, timeout=None) -> object
GuestAgentTransport.close()
QEMUSerialConsole(*, stream_factory=None, stream=None, owns_stream=False, resize=None)
QEMUSerialConsole.open(*, encoding=None, errors=None)
```

`GuestAgentTransport` sends one QGA request and returns its reply.
`QEMUSerialConsole` wraps a console byte stream (or a factory for one) for
`serial_console=`; `owns_stream` says whether closing the console closes an
injected `stream` (one from `stream_factory` is always closed).

## Serial consoles (`hostctl.transports.serial`)

```python
SerialConfig(device, *, baudrate=115200, bytesize=8, parity='N', stopbits=1, xonxoff=False, rtscts=False, dsrdtr=False, timeout=Timeout(), inter_byte_timeout=None, exclusive=None, profile=RawConsoleProfile(), serial_factory=None, serial_port=None)
SerialHost(config)
```

One exclusive byte-stream lease on a serial console; needs the `serial` extra
unless a port is injected. `device` is what pyserial takes (`"COM3"`,
`"/dev/ttyUSB0"`, `"rfc2217://host:4001"`). The URI path reads like a
`file:` URI's: `serial:///dev/ttyUSB0` (or `serial:/dev/ttyUSB0`) is
`/dev/ttyUSB0`; a Windows port and a pyserial URL drop the leading slash
(`serial:///COM3` is `COM3`, `serial:///%5C%5C.%5CCOM10` is `\\.\COM10`,
`serial:///loop://` is `loop://`); `serial:ttyUSB0` is a relative device; an
encoded slash after the leading one belongs to the device
(`serial:///%2Fdev%2FttyUSB0` is `/dev/ttyUSB0`); `serial:////...` raises
`ValueError`. `connection_uri` renders that readable form and reads back as the
same device. `serial_factory` (a `SerialFactory`, returning a `SerialLike`)
and `serial_port` are supported injection seams: a caller that passes
`serial_port=` keeps ownership of it. `SerialConfig` takes no
`username`/`password`; console credentials are the profile's `login=` steps.
RFC 2217 URLs are not encrypted. `timeout` (see "Timeouts"): `read` is
pyserial's read timeout (0.1 s), `write` its write timeout (10 s), `total`
the default of `run(timeout=)` (none); `connect` raises.
`Timeout(read=0)` is pyserial's non-blocking `timeout=0` (a read returns what
is already there) and `write=0` its `write_timeout=0`. `inter_byte_timeout`
is pyserial's own, separate field.

- `run()` works only when the profile advertises it, and refuses
  `executable=` and `bufsize=`. With a profile that declares a
  `shell_flavour` (`PosixConsoleProfile`), `host.shell_flavour` is that
  flavour, `host.shell` is a `Shell`, and `run()` renders every command
  (text, structured argv, `ShellOperator`s, a sole `Exec`) as ONE script in
  that flavour, run as one exchange: argv is quoted exactly as on any other
  host of the flavour. `cwd=`/`env=` are embedded and run in a subshell
  `( ... )`, so they never persist in the console's shell; plain commands
  still run in that shell. `CompletedProcess.args` is the script, with
  `env` values masked. With any other profile, `shell_flavour` raises
  `NotImplementedError`, each command is its own exchange, and a structured
  command, an `Exec`, `cwd=` and `env=` raise `NotImplementedError`.
- With a profile that has `execute` (`PosixConsoleProfile`), `input=` or a
  `stdin=` stream, read to its end, is the stdin of the command (of the
  whole script with a `shell_flavour`; one command only without), and
  stderr is separate unless `stderr=subprocess.STDOUT`; any other profile
  refuses `stdin=`/`input=` and merges the streams (`stderr` is `None`).
  There is no path or PTY.
- `spawn()` reads `bytes` unless `encoding=`/`errors=` asks for text.
- Negotiation is cached against the transport it ran on, so
  `host.executor.close()` forces a fresh login. Closing the host or the
  executor ends a live process: its reads and writes raise `ConnectionError`
  and its own `close()` is a no-op, so a stale handle never frees the lease of
  the process that replaced it.

```python
SerialProcess.read(size=-1, *, timeout=None) -> bytes
SerialProcess.write(data, *, timeout=None)
SerialProcess.send_break(duration=0.25)
SerialProcess.reset_input_buffer()
```

`SerialProcess` is the leased port as a `Process`, with `dtr`/`rts`
properties and `send_break()`. `SerialLike` is the pyserial-shaped port
protocol (`read`, `write`, `flush`, `send_break`, `close`). `profile=`
(default `RawConsoleProfile()`) takes a `hostctl.console` profile. The
process `spawn()` returns reads with `read(size=-1, *, timeout=None)` too,
so it is a `ConsoleStream` as it is.

## Consoles (`hostctl.console`)

A console profile drives a byte stream: it logs in, sends lines and, when it
can, frames one command's output and status. The profiles know nothing of the
transport; `SerialConfig(profile=)` uses one, and any other console stream can
be driven directly.

```python
ConsoleStream.read(size=-1, *, timeout=None) -> bytes
ConsoleStream.write(data)
console_stream(process) -> ConsoleStream
```

- `ConsoleStream` is the protocol a profile drives. `read` returns up to
  `size` bytes, `b""` when nothing arrived within `timeout` (`None`: the
  stream's own wait; `0`: only what is already there), and raises
  `ConnectionError` once the stream has ended; `write` delivers all of `data`.
  An optional `reset_input_buffer()` discards unread bytes.
- `console_stream(process)` returns a process whose `read` takes `timeout=`
  (a `SerialProcess`, a serial `spawn()` session) unchanged. Any other
  byte-mode `Process` -- `QEMUHost.open_serial()`, a container or SSH
  `spawn(terminal=True)` -- is read by a daemon thread from then on, so read it
  only through the returned stream. A process opened with `encoding=` raises
  `TypeError` on the first read.

```python
RawConsoleProfile()
PromptConsoleProfile(prompt, *, login=(), line_terminator=b'\r\n', error_patterns=(), status_marker=None, reliable_status=False, max_buffer=65536, read_size=4096, wakeup=b'\r', echo=True, paging_prompt=None, paging_continue=b' ', paging_disable=None, max_paging_pages=32, terminal_setup=None, status_parser=None, login_timeout=10.0)
LoginStep(expect, send, secret=False)
ConsoleProfile.negotiate(stream)
ConsoleProfile.run(stream, command, *, timeout=None) -> tuple[bytes, int]
ConsoleProfile.send(stream, command)
```

- `RawConsoleProfile` supports sessions only. It and `PromptConsoleProfile`
  have `shell_flavour = None`: a device's grammar is its own.
- `PromptConsoleProfile` adds bounded login/prompt framing and advertises
  `run` only when `reliable_status=True` and a `status_marker` is set.
  `login_timeout` bounds each login step and the first prompt (`ValueError`
  unless positive); it is not part of the URI. A failed login raises
  `ConsoleProtocolError`, never a bare `TimeoutError`, and a transcript
  attached to an error has `secret=True` values replaced with `<redacted>`.
- `LoginStep` is one expect/send pair; `ConsoleProfile` is the protocol a
  custom profile implements (`can_run`, `line_terminator`, `encoding` and the
  three methods). A timed-out `run` raises `TimeoutError` carrying the
  transcript so far as `.output`. An optional `shell_flavour` attribute (a
  `ShellFlavour`, or `None`) is not part of the protocol; a host reads it
  with `getattr` to quote argv and embed `cwd`/`env` (see `SerialHost.run`).

```python
PosixConsoleProfile(*, login=(), login_timeout=10.0, wakeup=b'\n', read_size=4096, shell='posix', batch=1024)
PosixConsoleProfile.shell_flavour -> PosixShellFlavour
PosixConsoleProfile.execute(stream, command, *, input=None, merge_stderr=False, timeout=None, on_stdout=None, on_stderr=None) -> tuple[bytes, bytes, int]
```

**`PosixConsoleProfile` is the profile to use for a POSIX shell's console**
(a Linux serial getty, a QEMU guest console, a container TTY, a web shell).
It never reads the echo: the command and its input are typed as base64
inside fixed shell syntax, so a ^C, TAB, `!` or newline in them is data;
stdout, stderr and the exit status come back between markers the shell
prints, each typed in two quoted halves so the echo can never contain one;
`stty -opost` makes the output exactly the program's bytes, and the
terminal's settings are restored afterwards.

- Needs sh-compatible syntax (sh, dash, bash, zsh, busybox ash, ksh) and
  `base64`, `mktemp`, `stty`, `cat`, `rm`. The command runs through `eval`
  in the console's own shell: `cd` and variables persist, and `exit` ends
  the session (the next read raises `ConnectionError`).
- `shell` names that shell's flavour (`"posix"`, `"bash"`, `"zsh"`, or a
  `PosixShellFlavour`), kept as `shell_flavour`; a host driving the profile
  quotes argv and embeds `cwd`/`env` with it. Pass `shell="zsh"` for a zsh
  console: zsh expands a bare `=word`, which POSIX quoting leaves unquoted.
  Any other flavour (fish, cmd, PowerShell) raises `ValueError`.
- `negotiate()` writes `wakeup`, runs the `login=` steps (as
  `PromptConsoleProfile`'s, each bounded by `login_timeout`), then a framed
  `:`; no answer within `login_timeout` raises `TimeoutError`, a non-zero
  status `ConsoleProtocolError`.
- `execute()` returns `(stdout, stderr, status)`. `input` (bytes or `str`)
  is the whole of stdin, delivered through a temporary file; without it
  stdin is `/dev/null`, so a command that reads stdin gets EOF instead of
  waiting on the terminal. `merge_stderr=True` is `2>&1`. `on_stdout` gets
  stdout as it arrives; stderr is delivered when the command ends. `run()`
  is `execute(merge_stderr=True)` without its stderr.
- Every typed line is under 1 KiB, and at most `batch` bytes (default 1024)
  are typed before the shell acknowledges them, so neither a minimal line
  editor nor a serial line's input queue loses a byte. A stream failure
  before the command line is typed raises `OperationNotStartedError`
  (nothing ran).
- `batch` (an int of at least 1024, or `None` for no acknowledgements)
  trades that round trip for safety. Keep the default on a serial line,
  which drops what overflows its 4 KiB input queue, and on a pty read and
  written from one thread, which stops reading input while its echo goes
  unread: a macOS pty after about 1.3 KiB (a Linux one takes 8 and 16 KiB).
  A stream that blocks the writer and whose far end keeps reading the
  terminal (a web shell, an SSH or container session) takes `None`: each
  acknowledgement costs a round trip, so the default spends one per KiB.
  Writes are at most 2 KiB, and what the console sent is read and dropped
  before each next one (it can only be echo and prompts until the command
  line is typed).
- On `timeout` it types ^C, which interrupts the command and discards the
  rest of the line, and raises `TimeoutError` with `.output` and `.stderr`
  so far; that command's temporary directory is left behind.

```python
class ConsoleExecutor(StreamingExecutor)
ConsoleExecutor(stream_factory, *, profile=None)
ConsoleExecutor._abandon(request, *, stream=None) -> bool
```

**`ConsoleExecutor` serves `run()` from a POSIX shell's console** -- a web
shell, a QEMU console, a container TTY -- through `PosixConsoleProfile`, with
the call surface of every executor ("Executor contract") and no thread.
Wrap it in an `ExecutorProvider`.

- `stream_factory()` returns the `ConsoleStream` of a console whose shell is
  ready to type into (log in there, `profile.negotiate(stream)`, if it needs
  it); it is called once per command. `profile` defaults to
  `PosixConsoleProfile()`; anything but a `PosixConsoleProfile` raises
  `TypeError`. Pass `PosixConsoleProfile(batch=None)` for a stream that
  cannot overflow.
- No native `args`, `cwd` or `env`: a host renders one line of shell text
  (`/bin/sh -c '...'` on a `PosixHost`), which runs in a child shell, so
  nothing a command does persists in the console's shell.
- stdout is delivered as it arrives, stderr when the command ends. One
  command holds the terminal from its first typed byte until its status
  arrives; another call waits for it within its own timeout (`TimeoutExpired`,
  `orphaned=False`, nothing typed).
- Decline rule: `OperationNotStartedError` only before the command line is
  typed -- `stream_factory` raised `OSError` or `TimeoutError` (any other
  exception propagates), or the stream failed while the command's files were
  typed. The host then tries its next provider. A failure after that is a
  `ConnectionError`, never retried elsewhere. The decline reason (the
  fallback WARNING, `SelectionStep.reason`) names the error on one line,
  credentials redacted: `console stream unavailable: ConnectionRefusedError:
  ...`, `console write failed before the command was typed: TimeoutError:
  ...`, `console stream failed before the command was typed: ...`.
- After a timeout, and after a failure once the stream was obtained (a
  decline included), `_abandon(request, stream=stream)` types ^C on the
  stream the factory returned for that command; a ^C write that raises is
  ignored (the stream is often abandoned because it failed). A timeout raises
  `TimeoutExpired` with the output so far and `orphaned=True` (^C stops only
  a program that heeds it); one that passed while acknowledgements were
  awaited, before the command line, is `orphaned=False`. Override `_abandon`
  to also drop `stream` and return `False` when that ends the command, as
  closing a login session does:

  ```python
  def _abandon(self, request, *, stream=None):
      super()._abandon(request, stream=stream)   # ^C, best effort
      session.drop(stream)
      return False
  ```

  It runs while the command still holds the terminal (before the next
  command's `stream_factory()` call), so `stream` is that command's. An
  exception it raises is swallowed and the command reported orphaned; the
  timeout or failure is what the caller sees. `stream=None` means the
  running command's stream.

## Shells (`hostctl.shell`)

A shell flavour renders commands for one shell language; a `Shell` binds one
to a host or executor.

```python
Shell(flavour, executor, *, cwd=None, env=None, encoding=None, errors=None)
Shell.run(*cmds, stdin=None, stdout=None, stderr=None, cwd=None, env={}, capture_output=None, check=None, encoding=None, errors=None, input=None, timeout=..., text=None)
Shell.session(*cmds, executable=None, cwd=None, env={}, terminal=None, encoding=None, errors=None) -> ShellSession
Shell.configure(*, cwd=None, env={}, encoding=None, errors=None) -> Shell
Shell.__call__(*args, cwd=None, env={}, encoding=None, errors=None) -> Shell
```

- `executor` is a host (an object with `run`) or an executor (a callable).
  Over a host, `Shell.run(x)` is `host.run(x)` -- the host renders one shell
  layer with its provider's capabilities -- and the flavour must be the
  host's own (`ValueError` otherwise). Over an executor, `run()` renders a
  script and sends it by the executor's capabilities: `args` gets the
  flavour's invocation as argv (a `CommandLine` for cmd), otherwise the script
  string; `cwd`/`env` are forwarded when the executor carries them natively
  and embedded when not. The subprocess options pass through.
- The constructor's `cwd`, `env`, `encoding` and `errors` are defaults for
  every call that omits them; they are read-only properties, as is
  `flavour`. A per-call `cwd`/`encoding`/`errors` replaces the default; `env`
  is an `EnvironmentSelection`: a mapping merges over the default per key, the
  default empty mapping inherits it, and `None` sends no environment at all
  (the host's own still applies -- it is not an empty environment).
- `host.shell(cwd=..., env=...)` and `configure(...)` return a configured copy
  without mutating the original. `Shell` is not an `Executor`: a positional
  argument to the call raises `TypeError`; run a program with argv through
  `host.executor(command, *args)` or `host.run(Exec(command, *args))`.
- `session()` opens a persistent provider process. `with host.shell as
  session:` opens a default session and closes it on exit; re-entering a
  shell whose session is open raises `RuntimeError`.

```python
ShellSession(flavour, process)
ShellSession.send(*cmds, cwd=None, env=None)
```

A `Process` that also speaks the flavour: `send()` takes the same command
grammar, writes the flavour terminator and a line terminator (interactive
shells need the newline to submit), and mutates the live shell context. TTY
stderr is merged into stdout.

Command grammar, shared by `Host.run()`, `Shell.run()` and `send()`:

- A raw string is shell text, verbatim; a tuple or list is one structured
  command whose elements are quoted as data; a path is a value that
  stringifies wherever it appears, so `run(p1, p2)` is two commands. Several
  top-level commands join with the flavour's `command_separator`; empty raw
  strings are skipped; empty structured commands and control characters are
  rejected.
- `ShellOperator.PIPE`, `.AND`, `.OR`, `.REDIRECT`, `.APPEND` and `.SEQUENCE`
  are infix tokens between top-level commands. After `REDIRECT`/`APPEND` the
  operand is a file name: raw text stays source, a path or one-element tuple
  is quoted for the shell itself. PowerShell 5 rejects `AND`/`OR`; PowerShell
  7 supports them. Windows PowerShell 5 writes a redirect as `| Out-File
  -LiteralPath NAME -Encoding Unicode [-Append]`, so `[`/`]` in the name are
  literal; pwsh keeps `>`.
- `ShellToken` is one element of that grammar; `ShellCommand(command,
  environment, stdin=None)` is a transport-ready rendering, whose
  `environment` is sent out of band (or `None` when embedded) and whose
  `stdin` carries a script sent on stdin.

```python
Exec(program, *args)
Cmdlet(name, *args)
PSParam(name)
```

- `Exec` is direct execution for `Host.run()`: one program plus argv, no
  shell layer. The program may be `str`, `bytes` or a path; all reach the
  transport as text, so a bare name resolves through the target's `PATH`. An
  `Exec` combined with any other command raises. It is a non-iterable frozen
  dataclass, so it is never quoted into an argv by mistake.
- `Cmdlet` is the PowerShell cmdlet spelling (PowerShell literals, no
  C-runtime escaping, nothing refused) and `PSParam("-Name")` is the one token
  rendered bare; a plain string is always a value. Other shells raise
  `TypeError` for either marker.

```python
ShellFlavour.script(cmds, *, cwd=None, env=None, epilogue=True) -> str
ShellFlavour.command(cmds, *, executable=None, cwd=None, env=None, login_shell=None) -> ShellCommand
ShellFlavour.invocation(script, *, executable=None)
ShellFlavour.quote(value) -> str
ShellFlavour.argument(value, *, target=ShellTarget.NATIVE) -> str
ShellFlavour.command_target(values) -> ShellTarget
ShellFlavour.escape_for_one_parse(text) -> str
ShellFlavour.environment_script(env) -> str
ShellFlavour.environment_assignment(key, value) -> str
ShellFlavour.redirect(value, target, operand) -> str
ShellFlavour.redirect_operand(value) -> str
PowerShellFlavour(major_version=5, executable=None)
PowerShellFlavour.command(cmds, *, executable=None, cwd=None, env=None, login_shell=None, stdin_free=False) -> ShellCommand
```

- `script()` builds one script in the flavour's language; `epilogue=False`
  leaves off the status epilogue, and `wants_epilogue(capabilities)` (false
  for a provider declaring `manages_status`) is how every caller decides.
- `command()` wraps a script for an SSH exec channel. `login_shell` names what
  parses the string first (`None`: nothing, as with `CreateProcess`;
  `"posix"`, `"cmd"`, `"powershell"`); `cmd` adds the second escape for a
  `cmd` login shell, which is `escape_for_one_parse()`, refused by default
  rather than guessed. A custom flavour must accept the keyword to be used
  over SSH. `invocation()` is the argv that runs a script.
- A flavour answers two quoting questions. `quote(value)` is syntactic:
  enough that this shell sees one word and expands nothing.
  `argument(value, target=)` quotes for whatever parses the token next, a
  `ShellTarget`: `SHELL` (a cmd builtin), `NATIVE` (a child's own argv parser
  -- the C runtime on Windows, nothing on POSIX), `CMDLET` (PowerShell's
  binder), `PROGRAM` (the program slot `CreateProcess` reads), `BATCH` (a batch
  file's `%*`/`%1`, which cmd parses again) or `UNRESOLVED` (a program that may
  be a batch file). On every POSIX-family shell `argument()` is `quote()`.
- `command_target(values)` classifies a Windows program by name: a cmd
  builtin is `SHELL`, `.exe`/`.com` `NATIVE`, `.bat`/`.cmd` `BATCH`, anything
  else `UNRESOLVED`. The first element of a structured command is the program
  and never argv data. A `BATCH` argument is one token (`"` + body + `"`, `"`
  -> `""`, `%` -> `%%cd:~,%`). An `UNRESOLVED` argument raises `ValueError`
  for a character a batch re-parse would act on (`"` on cmd; `" & | < >` on
  PowerShell). Windows PowerShell 5 raises for a native argument it would
  split; `PWSH` and `Exec` carry those. Under `cmd`, a structured `start`
  renders `start ""` and a structured `call` raises `ValueError`. On
  PowerShell 5 an absolute program path containing `[ ] * ?` is
  backtick-escaped after `&`.
- `environment_script(env)` renders a whole mapping by joining
  `environment_assignment()` per validated name (`export`, `$env:`,
  `set -gx`); values stay objects until the flavour stringifies them.
- `redirect()` renders a `REDIRECT`/`APPEND` with its target (`operand` is
  `redirect_operand(target)`); the default is `operator(value) + operand`.
- `cwd_guard` declares how a failed `cd` stops the payload: `"operator"`
  fuses the change onto a grouped payload with AND, `"statement"` is for a
  flavour whose change-directory statement aborts the script (PowerShell's
  `-ErrorAction Stop`).
- `repr()` of a flavour names it: `<PosixShellFlavour 'posix'>`.

```python
shell_flavour(value) -> ShellFlavour
shell_flavour_names() -> tuple[str, ...]
register_shell_flavour(value, *, name=None, replace=False) -> ShellFlavour
wants_epilogue(capabilities) -> bool
```

- `shell_flavour()` accepts a `ShellFlavourSelection`: a registered name, a
  configured `ShellFlavour` or a no-argument subclass.
  `shell_flavour_names()` lists the names it accepts, plugin entry points
  included. `register_shell_flavour()` adds a name; a taken name raises
  `ValueError` unless `replace=True`.
- Built-in instances: `POSIX_SHELL`, `BASH`, `ZSH`, `FISH`, `CMD`,
  `POWERSHELL` (Windows PowerShell 5) and `PWSH` (PowerShell 7), of the
  classes `PosixShellFlavour`, `BashShellFlavour`, `ZshShellFlavour`,
  `FishShellFlavour`, `CmdShellFlavour` and `PowerShellFlavour`. `"posix"`
  covers every sh-family shell and fish; csh and tcsh are unsupported.

## Executor contract (`hostctl.executor`)

```python
Executor.__call__(command, *args, stdin=None, stdout=None, stderr=None, cwd=None, env=None, capture_output=None, check=None, encoding=None, errors=None, input=None, timeout=None, text=None, **options)
```

The shell-agnostic surface every executor implements. `command` is a
`CommandLike` (`str | pathlib.PurePath | pathapi.Pathname`, kept a path
until the executor converts it); `*args` are `CommandArgument`s, which reach
an `ExecutorCapability.ARGS` executor as separate elements and never reach a
non-`ARGS` one, because the flavour already rendered them into `command`.
`ExecutionOptions` is the matching total-false `TypedDict`; transport
extensions stay keyword options. An executor declares
`executor_capabilities`, a set of strings: `ExecutorCapability.ARGS`, `CWD`,
`ENV` (native support), `SCRIPT` (takes a finalized script) and
`MANAGES_STATUS` (reports status itself, so no epilogue); a provider may
declare a capability this enum does not know. The aliases `CaptureOutput`,
`StreamTarget`, `CommandInput`, `StrPath` and `CommandEnvironment` type the
options. `CommandLine` is a `str` subclass marking a finalized command line,
which the executor passes through instead of quoting again.

```python
normalize_input(input, *, text_mode, encoding=None, errors=None)
normalize_environment(env) -> dict[str, str] | None
validate_environment_names(env)
capture_streams(capture_output, stdout, stderr)
dispatch_output(stdout_target, stderr_target, stdout, stderr, *, encoding, errors)
write_output(stream, value, *, encoding, errors)
reject_stdin_conflict(input, stdin)
wants_text(text, encoding, errors) -> bool
expired(command, timeout, *, output=None, stderr=None, orphaned=False, pid=None, text=False) -> subprocess.TimeoutExpired
```

The helpers that make an executor behave like the others; one that skips
them is where cross-transport divergence comes from.

- `normalize_input` matches `input` to the stream mode about to be used:
  bytes decoded under a text mode, `str` encoded under a binary one. A
  mismatch is not a clean error: `subprocess`'s writer thread dies without
  closing the pipe and the call blocks forever (`timeout=` does not fire).
  `bytes`, `bytearray`, `memoryview` and `str` are accepted; anything else
  raises `TypeError`.
- `normalize_environment` keeps `None` and turns everything else into a
  `str`-keyed dict, after `validate_environment_names`, which raises
  `ValueError("invalid environment variable name: ...")` outside
  `[A-Za-z_][A-Za-z0-9_]*`.
- `capture_streams` resolves the three spellings of "capture" into one
  `(stdout, stderr)` pair.
- `dispatch_output` routes both captured streams, and `write_output` one, to
  where the caller asked; `encoding` and `errors` are required keywords
  (pass `None`). A `None` target means `sys.stdout`/`sys.stderr`, not
  discard.
- `reject_stdin_conflict` refuses `input=` with `stdin=`, as `subprocess`
  does.
- `wants_text` is the one answer to "is this call in text mode": true when
  any of the three is set (`errors=` alone selects text).
- `expired` builds the `subprocess.TimeoutExpired` every transport raises:
  `.orphaned`, `.pid`, and output attributes that are never `None`.

### Writing an executor

```python
class BufferedExecutor(Executor[subprocess.CompletedProcess])
BufferedExecutor.executor_capabilities: frozenset[str] = frozenset()
BufferedExecutor.input_unsupported: str | None = None
BufferedExecutor.timeout_unsupported: str | None = None
BufferedExecutor.__call__(command, *args, bufsize=-1, stdin=None, stdout=None, stderr=None, cwd=None, env=None, capture_output=True, check=True, encoding=None, errors=None, input=None, timeout=..., text=None, **options) -> subprocess.CompletedProcess
BufferedExecutor._exchange(request) -> tuple[bytes | str | None, bytes | str | None, int | None]
BufferedExecutor._default_timeout() -> float | None
BufferedExecutor._normalize_error(exc) -> Exception
class StreamingExecutor(BufferedExecutor)
StreamingExecutor._send(request) -> None
StreamingExecutor._receive(request, wait) -> tuple[bytes, bytes, int | None]
StreamingExecutor._abandon(request) -> bool
ExecutorRequest(command, args=(), input=None, merge_stderr=False, cwd=None, env=None, timeout=None, deadline=None)
ExecutorRequest.remaining() -> float | None
```

The bases a third-party transport subclasses so it implements only its
primitive and behaves like every built-in executor. The `_` methods are
the override points (never called from outside); everything else is the
base's. Hand an instance to `ExecutorProvider(name, executor)`, which
advertises the class's `executor_capabilities`.

- `BufferedExecutor` is for a transport that returns a command's whole
  output: implement `_exchange`. `StreamingExecutor` is for one whose output
  arrives while the command runs: implement `_send` (deliver the command and
  its input) and `_receive` (wait at most `wait` seconds, `None` for
  indefinitely; return `(b"", b"", None)` when nothing arrived and a status
  when the command ended), plus `_abandon` when the transport can stop a
  command (return `False` once stopped; the default `True` reports it
  orphaned).
- `ExecutorRequest` (frozen) is what the primitive gets: `command` text,
  `args` as text (only with the `"args"` capability), `input` as the whole
  stdin in bytes or `None` for none (give the command an empty stdin),
  `merge_stderr` for `stderr=STDOUT` (merge natively if the transport can;
  stderr returned apart is appended), `cwd`/`env` (only with `"cwd"`/`"env"`;
  `env` validated and `str`-keyed), `timeout` in seconds or `None`, and
  `deadline` on `time.monotonic()`; `remaining()` is what is left of it.
- `__call__` refuses an unknown option with `TypeError`, and `args`, `cwd`
  or `env` the class does not declare with `NotImplementedError`. Set
  `input_unsupported`/`timeout_unsupported` to the reason a transport cannot
  take `stdin=`/`input=` or bound a call: either is then refused with
  `NotImplementedError(reason)`, input before the caller's stream is read.
  `input=` of any accepted type and a `stdin=` stream read to EOF both
  arrive as bytes.
- Output may be `bytes`, `str` or `None` (empty); the base converts it to
  the type `wants_text` selects, captures or routes it per
  `capture_streams`/`dispatch_output`, and returns a `CompletedProcess`
  whose `args` are `[command, *args]` with the `"args"` capability and the
  command text otherwise. A `None` status is `-1`; `check=True` is the
  default. A streaming executor writes uncaptured output as it arrives,
  decoding incrementally in text mode, and collects only captured output.
- `timeout` left out is `_default_timeout()` (`None`, no limit, unless
  overridden), so a host whose config total is `DEFAULT` gets the
  transport's own default; `None` is no limit. A streaming executor's
  deadline is enforced by the base: it calls `_abandon` and raises
  `TimeoutExpired` with the captured output so far. `_exchange` raises
  `subprocess.TimeoutExpired` itself; the base re-raises it via `expired()`
  with the call's command and output type, `orphaned=True` unless the
  exception says otherwise. Partial output torn mid-character decodes with
  `errors="replace"` unless `errors=` was given.
- `_normalize_error(exc)` maps any other transport exception (a returned
  `TimeoutExpired` is treated as a timeout). `OperationNotStartedError` lets
  the host try its next provider, so raise it only before the first byte
  was sent: from `_send` or from `_exchange` before it sends. Raised by
  `_receive` (or produced by `_normalize_error` there), the command is
  already out, so the base raises `ConnectionError` chained to it instead.
  Every failure other than a decline from `_send` calls `_abandon`.

## Processes (`hostctl.process`)

```python
Process.read(size=-1) -> ProcessData
Process.read_stderr(size=-1) -> ProcessData
Process.write(data)
Process.send_eof()
Process.wait(timeout=None) -> int
Process.terminate()
Process.kill()
Process.resize(columns, rows, pixel_width=0, pixel_height=0)
Process.close()
TerminalOptions(term_type='xterm-256color', columns=80, rows=24, pixel_width=0, pixel_height=0)
terminal_options(value) -> TerminalOptions | None
```

`Process` is the persistent-process protocol `spawn()` returns: a context
manager with `returncode`, reading and writing `ProcessData` (`str | bytes`,
by the `encoding=` it was opened with). `spawn(terminal=)` takes a
`TerminalRequest` (`True`, `False`, `None` or `TerminalOptions`), and
`terminal_options()` resolves one into `TerminalOptions` (`True` gives the
defaults) or `None`.

## Providers (`hostctl.provider`)

The names you implement when you add a transport, and the rules hostctl
applies to what you return.

```python
ExecutorProvider(name, executor, *, capabilities=None, probe=None)
PathProvider(name, factory, *, capabilities=None, probe=None)
ProviderProbe(availability, reason='', capabilities=frozenset(), system_hint=None)
SessionInitializer(initialize, timeout=None)
```

- `name` is what a trace and a `.via()` pin refer to; `executor` is any
  callable matching `Executor`; `factory` builds a `pathapi.Path` from
  segments. `capabilities` is a set of operation names -- strings, so an
  `ExecutorCapability` member and its spelling are interchangeable.
  `PathProvider.DEFAULT_CAPABILITIES` is the full vocabulary (`stat`,
  `scandir`, `open`, `open_read`, `open_write`, `read`, `write`, `exists`,
  `is_file`, `is_dir`, `mkdir`, `chmod`, `unlink`, `rmdir`, `rename`,
  `symlink_to`, `readlink`, `utime`, `hardlink_to`);
  `READ_ONLY_PATH_OPERATIONS` is the set of a content-only endpoint.
- `probe()` returns a `ProviderProbe`: `"available"`, `"degraded"` or
  `"unavailable"`; `usable` is true for the first two. A probe may narrow the
  declared capabilities, never add one.
- A provider's optional `connect()` is where a pre-dispatch failure belongs.
  Composite paths call a path provider's `connect()` before every dispatch, so
  it must be idempotent and cheap once connected.
- `OperationNotStartedError` is the only way to decline a provider for the
  generation, and means nothing was sent, so the next provider may be tried.
  Raise it before dispatch or not at all: a possibly-started operation is
  never replayed. A `NotImplementedError` from an operation that cannot mutate
  before raising falls through to the next provider for that call only.
- Teardown: `close()` closes a provider's `transport` attribute when it has one
  (providers sharing a transport share the attribute, so it closes once),
  otherwise the provider's own `close()`.
- `SessionInitializer` prepares a session (a login, a `cd`, an environment)
  once per connection rather than per call.

```python
ProviderSelector(providers=())
ProviderSelector.select(*, capability=None, exclude=(), pin=False) -> ProviderSelection
ProviderSelector.effective_capabilities(provider) -> frozenset
ProviderSelector.decline(name, reason='declined before dispatch', *, cause=None)
ProviderSelector.redact(value) -> str
ProviderSelection(provider, trace, generation=0, pinned=False)
SelectionStep(provider, availability, reason='', capabilities=(), chosen=False, generation=0, pin=False)
ProviderDetail(kind, provider, availability, reason='', capabilities=(), system_hint=None)
```

- `ProviderSelector` holds the ordered providers and the per-generation
  declines. `select()` re-admits a decline from an earlier operation only
  when no other provider can serve. `effective_capabilities()` is the declared
  set intersected with what the probe states (the declared set when it states
  none); selection, `host.capabilities`, `executor_capabilities`,
  `provider_details` and a composite path's dispatch all read it.
- A trace (`ProviderSelection.trace`, `host.last_selection`,
  `path.selection_trace`) is a tuple of frozen `SelectionStep` records in
  precedence order: `provider` is the redacted provider name, `chosen` whether
  that step selected it, `pin` whether the selection pinned. A provider that
  declined this generation appears as `availability="unavailable"` with the
  refusal as `reason`. `host.provider_details` is a tuple of frozen
  `ProviderDetail` records, executors first, with the effective capabilities.
- `redact(value)` is the redaction used in traces; rendered commands carry
  credentials, so log through it. It masks fish `set -gx NAME value` and
  quoted values holding `'`. An `env=` value embedded in command text is
  masked before rendering: the dispatch log, `CompletedProcess.args` and the
  `cmd` of `CalledProcessError`/`TimeoutExpired` carry `<redacted>` (the sent
  command is unchanged).

## Paths (`hostctl.paths`)

```python
CompositePosixPath.over(providers, *segments, backend=None) -> CompositePosixPath
CompositeWindowsPath.over(providers, *segments, backend=None) -> CompositeWindowsPath
CompositePosixPath.via(name)
CompositeWindowsPath.via(name)
```

`CompositePosixPath` and `CompositeWindowsPath` are the `pathapi.Path` types a
provider-backed host returns. They keep the logical path, select a provider
per operation, pin mutations and streams to the provider that started them,
and answer `pathapi`'s `_same_filesystem()` from their providers: two paths
are on one filesystem when they can route to the same provider object, so a
cross-host copy is never taken for a file copied onto itself, nor a file
copied onto itself for a cross-host copy. `.provider` is the selected
provider, `.providers` the ordered set, and `path.via(name)` a path pinned to
one. A composite copies (`copy.copy()`, sharing its providers) but
`pickle` and `copy.deepcopy()` raise `TypeError`: it is bound to live
providers.

- `over(providers, *segments, backend=None)` builds one with no host:
  `providers` is an iterable of `PathProvider`s in fallback order, or a
  `ProviderSelector`. It is the constructor `host.path()` calls, so per-
  operation fallback, `.via()`, `supports()`, forwarding and `backend=` (pin
  to that provider; unknown name `ValueError`, unavailable
  `OperationNotStartedError`) behave the same. A provider whose factory
  raises `OperationNotStartedError` is declined and the next one builds the
  path. Paths built over one `ProviderSelector` share its declines and probe
  cache; an iterable gets a selector of its own per call. A `str`, a lone
  provider or an empty set raises `TypeError`/`ValueError`. The path owns no
  provider: close them yourself (for `ssh_providers()`, the executor
  provider's `close()` closes the shared transport). The bare constructor
  still refuses plain segments with `TypeError`.

- `pathapi.protocols.supports(path, Protocol)` answers without a call that
  raises. A composite says yes to `Stat`, `Listing`, `BinaryOpen`, `Create`,
  `Remove`, `Chmod`, `Readlink`, `Symlink` or `Move` when some provider it may
  route to (only the pinned one after `.via()`) declares the operations
  behind it (`stat`, `scandir`, `open_read`, `mkdir`, `unlink` and `rmdir`,
  `chmod`, `readlink`, `symlink_to`, `rename`). `Utime`, `Hardlink`, `Owned`,
  `Resolve`, `NativeChecksum` and `Chown` also need the backend path class to
  implement them. No provider is probed, and the answer is advisory.
- WinRM, container and QGA backend paths share one base. `rename()` onto a
  path of another host raises `OSError(EXDEV)`, which `move()` takes as the
  cue to copy and delete; `chmod()` takes a mode int or an octal string;
  `_utime()` hands both times to the backend, so `touch()` of an existing
  file moves its time where the backend can set one. A
  write stream (`open("w")`, `"r+"`, `"a"`) uploads once on `close()` and is
  closed even when the upload fails, so write again to retry.
  `pathlib.Path(src).copy(dest)` onto such a path raises `FileExistsError`
  for an existing directory and `ValueError` for a source name that is not one
  path component.

## Transfer (`hostctl.sync`)

```python
stat_checksum(entry) -> tuple[int, float]
host_checksum(*hosts, algorithm='md5', chunk_size=1048576)
ProgressReader(reader, callback, *, total=None)
```

Helpers for `pathapi`'s `Path.copy()` and `pathapi.sync.PathSyncer`; hostctl
has no transfer engine of its own.

- `stat_checksum(entry)` is rsync's quick check: the cached `(size, mtime)`,
  reading no content. It misses a change that keeps both, and settles only on
  a target that takes a modification time: local, SSH and WinRM paths do, a
  QGA path with a guest helper and a container path with `touch` (or
  `powershell.exe`) do; elsewhere every sync copies again.
- `host_checksum(*hosts, ...)` returns a `PathSyncer` checksum that hashes a
  path on the host owning it (`<algorithm>sum` under a POSIX-family shell,
  `Get-FileHash` or `certutil` under PowerShell or cmd) rather than streaming
  it back. A local path is hashed in process, and a path no listed host owns,
  or whose host cannot hash it, is read through `open()`. `algorithm` is one
  of `md5`, `sha1`, `sha256`, `sha384`, `sha512`; an unknown one, no hosts,
  or `chunk_size <= 0` raises `ValueError`.
- `ProgressReader` wraps a binary reader and calls `callback(bytes_read,
  total)` as bytes are read; it is itself a binary reader and a context
  manager.

## Deploying Python (`hostctl.deploy`)

```python
deploy(host, source, path, *, mode='pyz', package=None, extras=(), skip=(), force=False, python='python3') -> Deployment
bundle(source, destination, *, mode='pyz', package=None, extras=(), skip=(), python='python3') -> pathlib.Path
Deployment(path, digest, written, command)
SourceTree(root, *, ignore_files=None, ignore_patterns=(), pythonpath=None)
SourceTree.files() -> list[tuple[str, pathlib.Path]]
SourceTree.requirements(extras=(), *, include=(), exclude=()) -> list[str]
```

Puts a Python program on a host that cannot install it itself (no `pip`, a
read-only root, no network). Needs the `deploy` extra; importing
`hostctl.deploy` does not, and the first `deploy()`, `bundle()` or
`SourceTree` read without it raises `MissingExtraError` naming the extra.

- `source` is an installed distribution's name (`"mytool"`), whose
  dependency closure is read from this environment's metadata, or a
  `SourceTree`. `package` is the import name run with `-m` (default: the
  distribution name with `-` as `_`; required for a tree, `TypeError`
  otherwise); it must be a dotted identifier (`ValueError`). `extras` are the
  distribution's extras to include (a dependency's own extras, like
  `pkg[fast]`, are always followed); `skip` names distributions never to
  ship. `python` is the target interpreter: `deploy()` probes with it, a
  zipapp's `#!` names it (as given when absolute, else via `/usr/bin/env`),
  a tree's launcher `exec`s it.
- `mode='pyz'` is one executable zipapp file; `mode='dir'` a directory with
  `lib/` (the packages) and `bin/<package>`, a `/bin/sh` launcher that puts
  `lib` first on `PYTHONPATH` (no empty entry, so the working directory is
  never added). A `SourceTree` ships only as `dir` (`BundleError`: a zipapp
  needs the package at the archive root). Another `mode` is `ValueError`.
- `deploy()` runs `python -` on the target with a stdlib-only probe that
  lists its installed distributions and its PEP 508 marker environment, then
  ships only what is missing, with markers evaluated for the target. A tree
  source is not probed: it vendors nothing.
- Payloads are reproducible (fixed timestamps, modes and ownership), so the
  SHA-256 `Deployment.digest` is stable across rebuilds. A deploy whose
  digest matches the one recorded on the target writes nothing
  (`written=False`) unless `force=True`.
- `path` is exactly where the zipapp file or the tree goes, and must be
  absolute (`ValueError`); a trailing `/` is dropped. A zipapp is written to
  `<path>.new` and renamed over `path`; its digest is `<path>.digest`. A tree
  is uploaded as `<path>.incoming.tar`, unpacked into `<path>.new` with its
  digest in `<path>/.digest`, and swapped in by renaming the old tree to
  `<path>.old`; a tree left only at `<path>.old` by an interrupted deploy is
  renamed back first. `deploy()` owns those sibling names and removes them.
- A `path` that exists without the digest marker is not a previous deploy
  and is never replaced: `FileExistsError`, with or without `force`.
- `Deployment(path, digest, written, command)`: `command` is the argv that
  runs the program, `(python, path)` for a zipapp and `(path + '/bin/' +
  package,)` for a tree; run it with `host.run(Exec(*deployment.command,
  *args))`.
- The target must be POSIX (`host.shell_flavour.pathname` is
  `PurePosixPath`; `NotImplementedError` otherwise, before anything is
  sent) with `mv`, `rm`, `mkdir`, `chmod` and `tar --strip-components`. Each
  step is an `Exec` argv with `--` before path operands, so no shell parses
  a path; a failing step raises `subprocess.CalledProcessError`. Progress is
  logged on `host.logger`.
- `BundleError` (before anything is sent): a distribution containing a
  compiled extension (`.so`, `.pyd`, `.dll`; install it on the target another
  way and `skip` it), a distribution with nothing importable, a tree without
  the package. A declared dependency not installed here is left out of the
  closure, not an error.
- `bundle()` writes the same layouts locally at `destination` and returns
  it; with no probe the whole closure ships, markers evaluated for this
  interpreter.
- `SourceTree(root, ...)` is a working tree shipped as a clone of it would
  have it: filtered by `.gitignore`, `.ignore` and `.bundleignore` (gitignore
  rules; nested files apply below their directory) or by the `ignore_files`
  named (a named file that is absent is `BundleError`); `ignore_patterns` are
  layered last, so `!name` re-includes. `.git` at any depth and symbolic
  links are never shipped. `pythonpath` names the tree's import roots
  relative to `root`; by default every one of `src`, `lib`, `vendor` and the
  root holding `<package>` is used. Its own dependencies are not vendored.
- `files()` lists `(arcname, source)` pairs, each arcname under the root's
  directory name. `requirements()` returns the declared dependency names
  from `pyproject.toml` (`dependencies` plus each of `extras`) or else
  `requirements.txt`, without installing anything; `include`/`exclude` take a
  name or `"[extra]"`, and `exclude` wins. A tree with neither file raises
  `BundleError`; `deploy()` then only logs that it declares nothing.

## Exceptions

Every exception hostctl raises of its own lives in `hostctl.exceptions` and
is re-exported from `hostctl`. Each also inherits the builtin a caller would
already catch, so `except ConnectionError` and `except TimeoutError` work
without naming a hostctl class. A bad argument is a plain `ValueError` or
`TypeError`; transport errors that already describe the failure (an
`OSError` from a path operation, `subprocess.TimeoutExpired` from `run()`)
pass through unwrapped.

```text
HostctlError(Exception)
├── TransportError(HostctlError, ConnectionError)
│   ├── OperationNotStartedError
│   └── QGADisconnectedError
├── ProtocolError(HostctlError, ConnectionError)
│   ├── QGAProtocolError
│   └── ConsoleProtocolError
├── QGATimeoutError(HostctlError, TimeoutError)
├── QGACommandError(HostctlError)
├── BundleError(HostctlError, RuntimeError)
└── MissingExtraError(HostctlError, ImportError)
```

```python
OperationNotStartedError(reason='operation was not started', *, cause=None)
QGACommandError(error_class, description, *, data=None)
MissingExtraError(extra, feature, *, python=None, distribution="hostctl")
```

- `HostctlError` -- the base; catch it for "anything hostctl raised".
- `TransportError` -- the connection to the target failed or was never made.
- `OperationNotStartedError` -- a provider declined before anything was sent
  (`.reason`, `.cause`); see "Providers". Raised to the caller when every
  provider declined, so an `except ConnectionError` retry around it is safe.
  It is an `OSError`, not a `RuntimeError`.
- `QGADisconnectedError` -- the guest-agent socket or channel closed, or could
  not be reached, before a reply.
- `ProtocolError` -- the peer answered, but not in the protocol's shape.
- `QGAProtocolError` -- a malformed, oversized or contract-violating guest
  agent reply.
- `ConsoleProtocolError` -- a serial console profile did not complete its
  login, prompt or status exchange.
- `QGATimeoutError` -- a guest-agent request missed its deadline.
- `QGACommandError` -- the guest agent refused a command (`.error_class`,
  `.description`, `.data`); path operations translate it into the matching
  `OSError`.
- `BundleError` -- `hostctl.deploy` cannot build a correct bundle from what
  is installed here; raised before anything is sent to the target.
- `MissingExtraError` -- a feature needs an optional extra that is not
  installed (`.extra`, `.feature`, `.distribution`): `FEATURE needs the
  'EXTRA' extra: pip install "DISTRIBUTION[EXTRA]"`. `distribution` is the
  package whose extra it is (default `"hostctl"`): a library built on hostctl
  raises this for its own extras and gets the CLI's exit 125 with them. With
  `.python` set (a `(major, minor)` pair) the extra cannot be installed on
  this interpreter (`psrp` before 3.10) and the message names that Python
  instead. It is an `ImportError`, so an existing
  `except ImportError` still catches it; an `ImportError` raised inside an
  installed dependency is never turned into one.

## Command line

```python
hostctl.cli.main(argv=None) -> int
```

The entry point of the `hostctl` console script and of `python -m hostctl`,
built on `duho`; install `hostctl[cli]`. Without the extra it raises
`SystemExit` naming it. `hostctl.cli` is not library API: `import hostctl`
never imports it, and it is absent from `hostctl.__all__`. Each subcommand is
a class in its own module (`hostctl.cli.cat.Cat`, `.cp.Cp`, `.info.Info`,
`.ls.Ls`, `.run.Run`, `.shell.Shell`, `.deploy.Deploy`).

```console
hostctl run local: -- python -c "print('hello')"
hostctl info ssh://server
hostctl ls ssh://server /etc
hostctl cat ssh://server /etc/os-release
hostctl cp ./artifact ssh://server:/tmp/artifact
hostctl shell ssh://server
hostctl deploy ssh://server mytool /opt/mytool.pyz -- --version
```

- The program after `run` follows `--`; it runs directly, without a shell.
- `run` and `info` take several hosts: more URI operands, and in any operand
  hosts separated by commas and `[1-3]`/`[a-c]` ranges (`duho.expand`,
  `[1-3:02d]` pads), outside the user part (a comma or bracket there is
  credentials). A later host with no scheme of its own takes the previous
  one's scheme and user (`ssh://admin@nas1,nas2`); write a literal comma
  elsewhere as `%2C`. `--parallel N` (default 1) works on N hosts at once.
  With more than one host, every `run` output line, stdout and stderr alike,
  starts with `[URI] ` (the URI without its password), `info` prints one
  `{"target": URI, "result": {...}}` line per host, each failing host gets a
  `hostctl: [URI] exit status N[: message]` line on stderr, and the exit
  status is the worst: 130, then the highest of 125-127, then the remote
  status largest in magnitude, then 0. A password is read once for all hosts.
  One host -- including an operand that expands to one -- is exactly the
  single-host command: no prefix, no wrapper, the same statuses.
- `ls` prints one name per line with backslashes and non-printable
  characters escaped (`\x1b`, `\u2028`); an undecodable byte is `\xNN`.
- `cp` operands are `URI:PATH` or a local path; the colon before the path is
  required even after a port (`ssh://host:2222:/x`).
- `deploy URI SOURCE PATH` is `hostctl.deploy.deploy()` (needs the `deploy`
  extra too): `--mode pyz|dir`, `--package`, `--extra` and `--skip`
  (repeatable), `--force`, `--python`; `--tree` makes SOURCE a directory
  (`SourceTree`) with `--ignore-file`, `--ignore-pattern` and `--pythonpath`
  (repeatable). Without `--` it prints `deployed PATH DIGEST12` or `current
  PATH DIGEST12`; arguments after `--` run the installed program, whose
  output and exit status become the command's. `BundleError`, a foreign
  `PATH` and a failed step on the host (`CalledProcessError`, reported with
  its last stderr line) exit 125.
- Each subcommand takes `-u`/`--username USERNAME`, `-p`/`--password
  PASSWORD`, `--ssh-config PATH`, `-v`/`-q` and `--loglevel`; the root
  takes `--version`. `--ssh-config` is always a file name (`''`: read no
  ssh_config); without it an SSH host reads `~/.ssh/config`, and a scheme
  with no ssh_config exits 125. A tool call takes it as `ssh_config`. `-u`
  replaces the URI's user and `-p` its password (`-p ''`: no password, no
  prompt), as the library's keyword credentials do. The password comes from
  `-p`, else `HOSTCTL_PASSWORD`, else the URI's own; without any, a host that
  asks for one gets a hidden prompt when standard input is a terminal (asked
  once for every host of a run), and otherwise fails at once with exit 126
  and a line naming `-p` and `HOSTCTL_PASSWORD`. A tool call takes `username`
  and `password` as parameters and never prompts. `-u` or `-p` on a scheme
  that takes no such credential exits 125. A login every provider refused
  exits 126 with the refusal's message.
- Exit status: `run` returns the remote command's own status; the command
  line's own failures are 125 (connection, timeout, usage, unsupported
  operation), 126 (permission denied), 127 (not found) and 130 (interrupted).
  A parser usage error is 2. A missing extra (`MissingExtraError`, never
  another `ImportError`) is 125 with one line, `hostctl: FEATURE needs the
  'EXTRA' extra: pip install "hostctl[EXTRA]"`. An unreachable host is 125 for every subcommand,
  `info` included.
- Under `HOSTCTL_MCP=stdio` every subcommand is an MCP tool (`hostctl.run`,
  `.ls`, `.cat`, `.cp`, `.info`, `.shell`, `.deploy`); `run`'s program and
  `deploy`'s arguments are the tool argument `"--"`. A tool call has no
  stdin, so `shell` there sends nothing.

## Environment variables

| variable | read by | when |
| --- | --- | --- |
| `HOSTCTL_PASSWORD` | the command line | on each subcommand without `-p`; the password is offered only to schemes whose config accepts one (`qemu+libvirt:` and `qga+unix:` take none), and replaces a password in the URI. A tool caller can read it and name any host to offer it to. |
| `HOSTCTL_MCP` | the command line | at start; `stdio` serves every subcommand as an MCP tool over stdio. |
| `HOME` (POSIX), `USERPROFILE` (Windows) | SSH | when a connection opens with `ssh_config=USER_SSH_CONFIG` (the default), to find `~/.ssh/config`, through `os.path.expanduser`. |
| `USERNAME` | native WinRM | when a password-free `WinRMConfig` opens on a Windows client: `username` must be this user (alone, `DOMAIN\user` or `user@DOMAIN`). Unset or empty raises `ValueError`. |
| `USERDOMAIN` | native WinRM | at the same moment, to accept the domain-qualified spellings of the current user; optional. |

The library reads nothing else from the environment; a transport's own
dependency may (asyncssh, pywinrm, docker), as documented by that package.

## Gotchas

- **One event-loop thread.** `run()`, `spawn()`, SFTP paths and connection
  setup over SSH (and a QGA tunnel over SSH) run asyncssh on one
  process-wide asyncio event loop in a daemon thread named
  `hostctl-asyncssh-loop`, started on first use; asyncssh objects are bound
  to it, and sftppath starts no loop thread for an SSH host's paths. It never
  touches the caller's event loop, so hostctl is usable from inside `async`
  code, but each call blocks the calling thread.
- **Fork.** An SSH host is not fork-safe in the sense of inheriting a usable
  connection: a child notices its loop changed, drops the inherited
  connection and SFTP channel without closing them (closing would tear down
  the parent's socket) and reconnects. `close()` in a forked child forgets
  the parent's connection.
- **SSH reads `~/.ssh/config` by default.** Like `ssh`, an `SSHConfig`
  (and `ssh://` on the command line) applies the user's own OpenSSH
  configuration: its `Host` aliases, `User`, `Port`, keys, `ProxyJump` and
  `UserKnownHostsFile`. Pass `ssh_config=None` for a connection that depends
  on no personal file (a service, a test). A `str` is configuration text,
  never a file name.
- **No replay after dispatch.** Provider failover happens only before
  anything is sent (`OperationNotStartedError`). A command that may have
  started is never retried on another provider, so a `ConnectionError`
  mid-command means its effect on the target is unknown.
- **A config's `timeout=` number is the total only.** `SSHConfig(h,
  timeout=60)` bounds each `run()`, not connecting: that is
  `timeout=Timeout(60, connect=10)`. Only `total` is a whole-command
  deadline; `connect`, `read` and `write` bound one step each. WinRM is the
  one transport whose `total` defaults to a number (30 s); `run(timeout=None)`
  lifts it for one call. A container has no `total` at all and refuses
  `run(timeout=<n>)`. A field the transport does not support raises rather
  than being ignored (the table in "Timeouts").
- **Loggers.** Every module logs through `logging.getLogger(__name__)`, so
  every logger is a child of `hostctl`: configure that one (at `DEBUG` it
  shows provider selection and dispatch, redacted). Child names follow the
  private module paths and are not API. hostctl installs no handler and sets
  no level. Records about one host (the fallback warning) go through
  `host.logger` and carry `record.host`; selection `DEBUG` records do not
  name a host.
- **netimps on first use.** `import hostctl` does not import `netimps`; the
  first call that needs it (a `ConnectionString`, a default port, a
  host-name check) does, and on Windows netimps then installs `recvmsg`,
  `sendmsg` and the `CMSG_*` helpers on the `socket` module unless its own
  `NETIMPS_SOCKET_PATCH=0` was set before that import.
- **`check=True` by default.** `run()` raises `CalledProcessError` on a
  non-zero status, unlike `subprocess.run`.
- **Paths taken before `close()`.** A container or QGA path handed out before
  its host closed refuses rather than reconnecting, where an SSH path runs on
  the host's next connection; a path from before `replace_providers()` keeps
  the old providers. Obtain paths again.
