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
`hostctl.sync`, `hostctl.cli` and `hostctl.transports.local`, `.ssh`,
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
raises naming the extra to install. `hostctl.has_ssh()` and
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
  reaches a field that `connection_uri` or `repr()` renders. A password both
  in the URI and as a keyword raises `ValueError`. The password field is split
  by `parse_credentials` (see "URIs"), so a second factor travels in it; an
  extra the config does not declare is rejected by name.
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
Host.run(*cmds, bufsize=-1, executable=None, stdin=None, stdout=None, stderr=None, cwd=None, env=None, capture_output=True, check=True, encoding=None, errors=None, input=None, timeout=None, text=None) -> subprocess.CompletedProcess
Host.path(*segments, backend=None) -> pathapi.Path
Host.spawn(*cmds, executable=None, cwd=None, env=None, terminal=None, encoding=None, errors=None) -> Process
Host.info() -> HostInfo
Host.connect()
Host.close()
```

- `Host` is abstract; `run()`, `path()` and `spawn()` raise
  `NotImplementedError` on a host that cannot do them. A host also exposes
  `scheme`, `connection_uri`, `capabilities`, `executor`,
  `executor_capabilities`, `last_selection` (the redacted provider trace of
  the most recent `run()`), `shell_flavour` and `shell`, and is a context
  manager.
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
SystemConfig(authority='localhost', *, shell=None, executor=(), path=(), provider_options=None, initializer=None)
PosixConfig(authority='localhost', *, shell=None, executor=(), path=(), provider_options=None, initializer=None)
WindowsConfig(authority='localhost', *, shell=None, executor=(), path=(), provider_options=None, initializer=None)
IOSConfig(authority='localhost', *, shell=None, executor=(), path=(), provider_options=None, initializer=None)
```

The `posix://`, `windows://` and `ios://` configs compose a system host from
provider descriptors instead of owning a transport. `executor=` and `path=`
are sequences of descriptor names (a bare string raises `TypeError`); the
built-ins are `local`, `ssh`, `sftp` and `winrm`, and a transport descriptor
needs its config in `provider_options` (`{"ssh": SSHConfig(...)}`), which
never serializes credentials into the canonical URI. `initializer` is a
`SessionInitializer` or callable run once per connection generation.

```python
SystemHost(config=None, *, executor_providers=(), path_providers=(), shell=None, info=None, initializer=None)
PosixHost(config=None, *, executor_providers=(), path_providers=(), shell=None, info=None, initializer=None)
WindowsHost(config=None, *, executor_providers=(), path_providers=(), shell=None, info=None, initializer=None)
IOSHost(config=None, *, executor_providers=(), path_providers=(), shell=None, info=None, initializer=None)
SystemHost.replace_providers(*, executor=None, path=None)
SystemHost.runspace()
```

- A system host takes a `HostConfig`, never a connection string, and the
  providers to compose. `config_type` names the config class a host belongs
  to; constructed with no config, a host builds `config_type()` (`PosixHost()`
  a `PosixConfig()`). `IOSHost` is session/command-only: it has no path
  grammar, so `path()` raises.
- `executor_providers` and `path_providers` are the ordered tuples the host
  was composed from; `provider_details` reports availability without
  dispatching.
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

`register_system_provider` adds a descriptor: `resolver(config, kind)` returns
the provider for `kind` (`"executor"` or `"path"`) or `None`. A name already
registered raises `ValueError` unless `replace=True`; an unknown descriptor
loads the `hostctl.system_providers` entry point of that name.
`system_provider_names()` lists what is registered, entry points included
without importing them.

## Local (`hostctl.transports.local`)

```python
LocalConfig()
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
SSHConfig(host, *, port=22, username=None, password=None, client_keys=None, executable=None, known_hosts=(), shell=POSIX_SHELL, pathname=PosixPathname, login_shell='auto', connect_timeout=30.0, ssh_config=None, connect_options={})
ssh_providers(config) -> tuple[ExecutorProvider, PathProvider]
```

`ssh://user@host:port`; needs the `ssh` extra. Opening it returns a
`PosixHost` (or `WindowsHost` for a Windows path flavour) whose `run()` uses
an AsyncSSH exec channel and whose paths use an `sftppath.SFTPPath` backend.

- `username=None` leaves the account to asyncssh (the local user, as `ssh`
  does). `client_keys` is an `SSHClientKeys` (one `SSHKeySource` -- a key
  path, bytes or text -- or a sequence of them); `known_hosts` an
  `SSHKnownHosts`, the same shape. Authentication fields are excluded from
  `repr()`.
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
- `connect_timeout` bounds establishing the connection, in seconds (`None`
  keeps asyncssh's default; otherwise it must be > 0); it is the URI key
  `connect_timeout`, omitted from `connection_uri` at 30.
- `ssh_config` is the one way to read an ssh_config, and both legs read it.
  Without it neither leg reads one -- both get `config=None`, so an alias,
  `IdentityFile`, `UserKnownHostsFile` or `ProxyCommand` in `~/.ssh/config` is
  ignored. It is never a URI key, and an `ssh://` URI naming it raises: a
  `ProxyCommand` starts a local process, which a URI's author must not choose.
- `connect_options` are extra `asyncssh.connect` keywords for both legs, kept
  out of the URI and `repr()`; a key hostctl owns (`host`, `port`,
  `username`, `password`, `client_keys`, `known_hosts`, `config`,
  `connect_timeout`) raises and names the field to use.
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

- `connection` is the asyncssh connection `run()` and `spawn()` use, for
  port forwarding or an SFTP client of your own. It connects on first access
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

- A composed SSH host opens two connections, each when first needed and each
  authenticating once: the exec connection on the first command (or relative
  path), the SFTP connection on the first path operation. The SFTP leg gets
  the exec leg's connect options, so both reach the same server and verify
  its key the same way. A host used only for absolute paths never opens the
  exec one.
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
- See "Gotchas" for fork behaviour and the event-loop threads.

## WinRM (`hostctl.transports.winrm`)

```python
WinRMConfig(host, *, username, password=None, auth='ntlm', port=None, tls=False, verify=True, message_encryption='auto', operation_timeout=20.0, read_timeout=30.0, provider='auto', allow_cleartext=False)
winrm_providers(config) -> tuple[ExecutorProvider, PathProvider]
```

`winrm://` (HTTP) and `winrms://` (HTTPS); needs the `winrm` extra, `psrp` for
runspaces. Opening it returns a `WindowsHost` running PowerShell, whose paths
are a `CompositeWindowsPath` over the WinRM path backend.

- `auth` is the scheme (`ntlm`, `kerberos`, `credssp`, `basic`, `plaintext`,
  `ssl`; CredSSP and Kerberos need `winrm-credssp`/`winrm-kerberos`).
  `basic`, `plaintext` or `ssl` over plain HTTP sends the password in
  cleartext and raises `ValueError` unless `allow_cleartext=True` (URI
  `?allow_cleartext=true`).
- `tls=True` is HTTPS; `verify=False` (`?verify=false`) skips the server
  certificate check. `str(config)` omits an unset port.
- The timeouts are float seconds, rounded up where a leg needs whole seconds.
- `provider` is `auto`, `native`, `pywinrm` or `psrp`. `auto` picks a
  password-free config on a Windows client as `native` (current-context
  PowerShell remoting, see "Environment variables"); otherwise PSRP when
  `hostctl[psrp]` is installed on Python 3.10+, otherwise pywinrm. `native`
  takes no password; `pywinrm`/`psrp` need one (or `auth="kerberos"`);
  `provider="psrp"` without the extra raises the install hint.
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
- The native leg's `read_timeout` is a total per-call deadline that
  `run(timeout=)` overrides; the remote command keeps running
  (`orphaned=True`). pywinrm and PSRP have no total deadline and refuse
  `timeout=`. On the native leg `verify=False` becomes `New-PSSessionOption
  -SkipCACheck -SkipCNCheck`, and the remote exit code is carried back
  explicitly.
- PSRP `run()` text is rendered by `Out-String -Stream` in the runspace
  (tables as on the other legs, CRLF-joined, 120 columns).
- A WinRM path's `open("rb")` fetches bounded ranges; writable modes stage
  content and transfer Base64 chunks on close (see "Paths").

```python
RunspaceSession(config=None, *, pool=None)
RunspaceSession.invoke(script, *args, raw=False, capture_exit=False, format_text=False) -> PipelineResult
RunspaceSession.connect()
RunspaceSession.close()
PipelineResult(output, streams, state, had_errors, returncode)
PipelineStreams(error=(), warning=(), verbose=(), debug=(), information=(), progress=())
```

A PSRP runspace with typed PowerShell streams and state, reached through
`host.runspace()`; it is not a byte-oriented `spawn`/TTY process.
`invoke(script, *args)` renders its arguments with the PowerShell flavour's
`structured_command` and returns typed objects in `PipelineResult.output`. An
injected `pool` is caller-owned: `close()` leaves it open and the session stays
reopenable.

## Containers (`hostctl.transports.container`)

```python
ContainerConfig(container, *, engine_url=None, username=None, workdir=None, executable=None, shell='auto', pathname='auto', client_factory=None)
ContainerHost(config, *, executor_providers=(), path_providers=())
```

A Docker Engine container; needs the `container` extra. Inspection selects
Linux/POSIX or Windows/PowerShell semantics. `ContainerHost` supports
buffered exec, persistent sessions and TTYs, and archive-backed POSIX or
Windows paths. `client_factory` builds the Docker client (default:
`docker.DockerClient` for an `engine_url`, otherwise `docker.from_env`).

- `run()` takes no stdin and no `timeout=`: Docker's buffered exec streams
  neither, and the exec API has no cancellable deadline.
- Archive paths support stat, traversal, read, write, append,
  exclusive-create, `symlink_to()` and `readlink()`; `mkdir`, remove, rename
  and `chmod` raise `NotImplementedError`. `iterdir()`/`walk()` pull one
  archive per directory, so list a large tree through `run()`.
- An overwrite keeps the existing file's owner and mode; a new file is uid/gid
  0, mode 0644, and `username=` applies to exec only. `exists()` and
  `open("x")` read one member header. A path taken before
  `ContainerHost.close()` refuses (`ValueError`) instead of reopening the
  client.

## QEMU guest agent (`hostctl.transports.qemu`)

```python
QEMUConfig(domain, *, transport='libvirt', connection=None, socket_path=None, ssh=None, agent_timeout=10.0, max_reply_size=48*1024*1024, shell='auto', pathname='auto', transport_factory=None, serial_console=None, path_helper=None)
QEMUHost(config)
QEMUHost.open_serial(*, encoding=None, errors=None) -> Process
```

A guest reached through the QEMU guest agent (QGA). `transport` is a
`QEMUTransport`: `"libvirt"` (local libvirt, the `qemu-libvirt` extra),
`"unix"` (a socket at `socket_path`) or `"ssh"` (a remote Unix socket over an
AsyncSSH tunnel described by `ssh=SSHConfig(...)`). `transport_factory`
returns a `GuestAgentTransport` instead. Discovery positively probes QGA and
its enabled command list.

- `run()` uses buffered `guest-exec`/`guest-exec-status` and declares argv
  only: `guest-exec`'s environment replaces the guest's, so the host embeds
  assignments in the rendered script and a direct `Exec` refuses
  `cwd=`/`env=`. QGA cannot cancel a timed-out process: `orphaned` is true and
  `.pid` is kept. `run(stderr=STDOUT)` interleaves the streams on qemu-ga 8.0+
  with a non-Windows guest and otherwise returns stdout then stderr. Results
  carry `stdout_truncated`/`stderr_truncated` and warn when set.
- `max_reply_size` bounds one QGA reply; `agent_timeout` bounds one round trip
  independently of a command's `timeout=`: an agent that stops answering
  raises `ConnectionError`, not a command timeout.
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
- A guest-agent error reply raises `QGACommandError`, which path operations
  translate into the matching `OSError`; a malformed or oversized reply
  raises `QGAProtocolError` (see "Exceptions").
- An injected `serial_console` adds the `serial` capability and
  `open_serial()`: a raw, exclusive console with no shell or status claim;
  `encoding=` makes it read and write `str`.

```python
GuestPathHelper.stat(path, *, follow_symlinks=True)
GuestPathHelper.scandir(path)
GuestPathHelper.mkdir(path, mode)
GuestPathHelper.unlink(path, *, missing_ok=False)
GuestPathHelper.rmdir(path)
GuestPathHelper.rename(path, target, *, replace=False)
GuestPathHelper.chmod(path, mode, *, follow_symlinks=True)
PosixGuestPathHelper(run)
PosixGuestPathHelper.probe(run)
WindowsGuestPathHelper.probe(run)
```

`GuestPathHelper` is the protocol `QEMUConfig(path_helper=...)` takes; without
one, discovery probes a built-in. `run(program, *args, **options)` returns a
`subprocess.CompletedProcess` (a QEMU host's `executor` is one). `probe(run)`
returns a helper, or `None` when the guest cannot support it, and the guest
keeps its read/write-only paths.

- `PosixGuestPathHelper` drives GNU `stat -c` and `find -printf` (proven by
  `stat -c %f -- /` answering hex). Every path is an argv element after `--`,
  so a leading `-` is data and nothing is interpolated into shell text;
  `find` takes no `--`, so it gets an absolute path or one anchored with `./`.
  Of a regular file, `scandir` lists nothing.
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
SerialConfig(device, *, baudrate=115200, bytesize=8, parity='N', stopbits=1, xonxoff=False, rtscts=False, dsrdtr=False, read_timeout=0.1, write_timeout=10, inter_byte_timeout=None, exclusive=None, profile=RawConsoleProfile(), serial_factory=None, serial_port=None)
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
RFC 2217 URLs are not encrypted.

- `run()` frames each command as its own profile exchange, only when the
  profile advertises it; it refuses a structured (argv) command (the host
  names no shell flavour), and `stdin=`, `bufsize=` and `input=`. Streams are
  merged; there is no path or PTY.
- `spawn()` reads `bytes` unless `encoding=`/`errors=` asks for text.
- Negotiation is cached against the transport it ran on, so
  `host.executor.close()` forces a fresh login. Closing the host or the
  executor ends a live process: its reads and writes raise `ConnectionError`
  and its own `close()` is a no-op, so a stale handle never frees the lease of
  the process that replaced it.

```python
RawConsoleProfile()
PromptConsoleProfile(prompt, *, login=(), line_terminator=b'\r\n', error_patterns=(), status_marker=None, reliable_status=False, max_buffer=65536, read_size=4096, wakeup=b'\r', echo=True, paging_prompt=None, paging_continue=b' ', paging_disable=None, max_paging_pages=32, terminal_setup=None, status_parser=None, login_timeout=10.0)
LoginStep(expect, send, secret=False)
ConsoleProfile.negotiate(process)
ConsoleProfile.run(process, command, *, timeout=None) -> tuple[bytes, int]
ConsoleProfile.send(process, command)
```

- `RawConsoleProfile` supports sessions only.
- `PromptConsoleProfile` adds bounded login/prompt framing and advertises
  `run` only when `reliable_status=True` and a `status_marker` is set.
  `login_timeout` bounds each login step and the first prompt (`ValueError`
  unless positive); it is not part of the URI. A failed login raises
  `ConsoleProtocolError`, never a bare `TimeoutError`, and a transcript
  attached to an error has `secret=True` values replaced with `<redacted>`.
- `LoginStep` is one expect/send pair; `ConsoleProfile` is the protocol a
  custom profile implements.

```python
SerialProcess.read(size=-1, *, timeout=None) -> bytes
SerialProcess.write(data, *, timeout=None)
SerialProcess.send_break(duration=0.25)
SerialProcess.reset_input_buffer()
```

`SerialProcess` is the leased port as a `Process`, with `dtr`/`rts`
properties and `send_break()`. `SerialLike` is the pyserial-shaped port
protocol (`read`, `write`, `flush`, `send_break`, `close`).

## Shells (`hostctl.shell`)

A shell flavour renders commands for one shell language; a `Shell` binds one
to a host or executor.

```python
Shell(flavour, executor, *, cwd=None, env=None, encoding=None, errors=None)
Shell.run(*cmds, stdin=None, stdout=None, stderr=None, cwd=None, env={}, capture_output=None, check=None, encoding=None, errors=None, input=None, timeout=None, text=None)
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
```

- `ProviderSelector` holds the ordered providers and the per-generation
  declines. `select()` re-admits a decline from an earlier operation only
  when no other provider can serve. `effective_capabilities()` is the declared
  set intersected with what the probe states (the declared set when it states
  none); selection, `host.capabilities`, `executor_capabilities`,
  `provider_details` and a composite path's dispatch all read it. Trace
  entries carry a constant `"policy": "ordered"`.
- `redact(value)` is the redaction used in traces; rendered commands carry
  credentials, so log through it. It masks fish `set -gx NAME value` and
  quoted values holding `'`. An `env=` value embedded in command text is
  masked before rendering: the dispatch log, `CompletedProcess.args` and the
  `cmd` of `CalledProcessError`/`TimeoutExpired` carry `<redacted>` (the sent
  command is unchanged).

## Paths (`hostctl.paths`)

```python
CompositePosixPath.via(name)
CompositeWindowsPath.via(name)
```

`CompositePosixPath` and `CompositeWindowsPath` are the `pathapi.Path` types a
provider-backed host returns. They keep the logical path, select a provider
per operation, pin mutations and streams to the provider that started them,
and answer `pathapi`'s `_same_filesystem()` with the host's provider set, so a
cross-host copy is never taken for a file copied onto itself. `.provider` is
the selected provider and `path.via(name)` a path pinned to one. A composite
copies but cannot be pickled: it is bound to a live host.

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
  cue to copy and delete; `chmod()` takes a mode int or an octal string. A
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
  a target that takes a modification time (local and SSH paths do; WinRM,
  container and QGA paths do not, so every sync there copies again).
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
└── QGACommandError(HostctlError)
```

```python
OperationNotStartedError(reason='operation was not started', *, cause=None)
QGACommandError(error_class, description, *, data=None)
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

## Command line

```python
hostctl.cli.main(argv=None) -> int
```

The entry point of the `hostctl` console script and of `python -m hostctl`,
built on `duho`; install `hostctl[cli]`. Without the extra it raises
`SystemExit` naming it. `hostctl.cli` is not library API: `import hostctl`
never imports it, and it is absent from `hostctl.__all__`. Each subcommand is
a class in its own module (`hostctl.cli.cat.Cat`, `.cp.Cp`, `.info.Info`,
`.ls.Ls`, `.run.Run`, `.shell.Shell`).

```console
hostctl run local: -- python -c "print('hello')"
hostctl info ssh://server
hostctl ls ssh://server /etc
hostctl cat ssh://server /etc/os-release
hostctl cp ./artifact ssh://server:/tmp/artifact
hostctl shell ssh://server
```

- The program after `run` follows `--`; it runs directly, without a shell.
- `ls` prints one name per line with backslashes and non-printable
  characters escaped (`\x1b`, `\u2028`); an undecodable byte is `\xNN`.
- `cp` operands are `URI:PATH` or a local path; the colon before the path is
  required even after a port (`ssh://host:2222:/x`).
- Each subcommand takes `--ask-password`, `-v`/`-q` and `--loglevel`; the
  root takes `--version`. `--ask-password` is refused in a tool call.
- Exit status: `run` returns the remote command's own status; the command
  line's own failures are 125 (connection, timeout, usage, unsupported
  operation), 126 (permission denied), 127 (not found) and 130 (interrupted).
  A parser usage error is 2. An unreachable host is 125 for every subcommand,
  `info` included.
- Under `HOSTCTL_MCP=stdio` every subcommand is an MCP tool (`hostctl.run`,
  `.ls`, `.cat`, `.cp`, `.info`, `.shell`); `run`'s program is the tool
  argument `"--"`. A tool call has no stdin, so `shell` there sends nothing.

## Environment variables

| variable | read by | when |
| --- | --- | --- |
| `HOSTCTL_PASSWORD` | the command line | on each subcommand; the password is offered only to schemes whose config accepts one (`qemu+libvirt:` and `qga+unix:` take none). A tool caller can read it and name any host to offer it to. |
| `HOSTCTL_MCP` | the command line | at start; `stdio` serves every subcommand as an MCP tool over stdio. |
| `USERNAME` | native WinRM | when a password-free `WinRMConfig` opens on a Windows client: `username` must be this user (alone, `DOMAIN\user` or `user@DOMAIN`). Unset or empty raises `ValueError`. |
| `USERDOMAIN` | native WinRM | at the same moment, to accept the domain-qualified spellings of the current user; optional. |

The library reads nothing else from the environment; a transport's own
dependency may (asyncssh, pywinrm, docker), as documented by that package.

## Gotchas

- **Two event-loop threads.** `run()`, `spawn()` and connection setup over
  SSH (and a QGA tunnel over SSH) run asyncssh on one process-wide asyncio
  event loop in a daemon thread named `hostctl-asyncssh-loop`, started on
  first use; asyncssh objects are bound to it. The SSH path leg is sftppath's,
  which runs asyncssh on a loop thread of its own. Neither touches the
  caller's event loop, so hostctl is usable from inside `async` code, but each
  call blocks the calling thread.
- **Fork.** An SSH host is not fork-safe in the sense of inheriting a usable
  connection: a child notices its loop changed, drops the inherited connection
  without closing it (closing would tear down the parent's socket) and
  reconnects. `close()` in a forked child forgets the parent's connection.
- **No replay after dispatch.** Provider failover happens only before
  anything is sent (`OperationNotStartedError`). A command that may have
  started is never retried on another provider, so a `ConnectionError`
  mid-command means its effect on the target is unknown.
- **Timeouts are not all total deadlines.** `run(timeout=)` bounds the call on
  local, SSH, QEMU and native WinRM. `SSHConfig(connect_timeout=)` bounds only
  connecting; `QEMUConfig(agent_timeout=)` one agent round trip;
  `WinRMConfig(operation_timeout=, read_timeout=)` are per-request on pywinrm
  and PSRP, which refuse `timeout=`; a container's `run()` refuses `timeout=`;
  `SerialConfig(read_timeout=)` bounds one read.
- **Loggers.** Every module logs through `logging.getLogger(__name__)`, so
  every logger is a child of `hostctl`: configure that one (at `DEBUG` it
  shows provider selection and dispatch, redacted). Child names follow the
  private module paths and are not API. hostctl installs no handler and sets
  no level.
- **netimps on first use.** `import hostctl` does not import `netimps`; the
  first call that needs it (a `ConnectionString`, a default port, a
  host-name check) does, and on Windows netimps then installs `recvmsg`,
  `sendmsg` and the `CMSG_*` helpers on the `socket` module unless its own
  `NETIMPS_SOCKET_PATCH=0` was set before that import.
- **`check=True` by default.** `run()` raises `CalledProcessError` on a
  non-zero status, unlike `subprocess.run`.
- **Paths taken before `close()`.** A container or QGA path handed out before
  its host closed refuses rather than reconnecting; a path from before
  `replace_providers()` keeps the old providers. Obtain paths again.
