# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Migrating from 0.3

Every row ships in 0.4.0. No old name is kept as an alias. Entries further
down that predate these renames keep the 0.3 spellings; this table maps them.

| 0.3 | 0.4.0 |
| --- | --- |
| `OperationNotStarted` (a `RuntimeError`) | `OperationNotStartedError` (a `ConnectionError`) |
| `QgaCommandError`, `QgaProtocolError`, `executor.GuestAgentProtocolError` | `QGACommandError`, `QGAProtocolError` |
| `QgaTimeoutError`, `QgaDisconnectedError`, `QgaError` (private module) | `QGATimeoutError`, `QGADisconnectedError`; catch `HostctlError` |
| `ConsoleProtocolError` from `hostctl.serial` | same name, `hostctl.exceptions`, under `HostctlError` |
| `SshConfig`, `QemuConfig`, `QemuHost`, `IosConfig`, `IosHost` | `SSHConfig`, `QEMUConfig`, `QEMUHost`, `IOSConfig`, `IOSHost` |
| `WinRMPath`, `HostPath` in `hostctl` | removed; `host.path()` returns `CompositeWindowsPath`; annotate with `pathapi.Path` |
| `from hostctl.host import PosixHost, uri_host, ...` | `from hostctl import ...` (homes: `hostctl.system`, `hostctl.uri`; `hostctl.host` holds only `Host`, `HostConfig`, `HostInfo`, `Timeout`, `DEFAULT` and the selections) |
| `hostctl.host.qemu`, `.container`, `.serial`, `.system`, `.composite_path` | `hostctl.transports.qemu`, `.container`, `.serial`, `hostctl.system`, `hostctl.paths` |
| `hostctl.serial` (console profiles) | `hostctl.transports.serial` (still re-exported from `hostctl`) |
| concrete executors, processes, path backends, `provider.transports`, `shell.posix` etc.; `LocalExecutor` from `hostctl.executor` | internal; flavour classes stay in `hostctl.shell`; `LocalExecutor` is in `hostctl` and `hostctl.transports.local` |
| `provider.transports.READ_ONLY_PATH_OPERATIONS` | `hostctl.provider.READ_ONLY_PATH_OPERATIONS` |
| loggers `hostctl.host.ssh`, `hostctl.host.winrm`, `hostctl.host.system`, `hostctl.host.qemu`, `hostctl.provider`, `hostctl.provider.transports` | one per module, named for it (`hostctl.transports.ssh._transport`, `hostctl.system._host`, `hostctl.provider._contracts`, ...); configure `hostctl` |
| `dialect=`, `path_flavor=` (and `?dialect=`, `?path_flavor=`); `ShellFlavour.path_flavor` | `shell=`, `pathname=` (`?shell=`, `?pathname=`); `ShellFlavour.pathname` |
| `SSHShellSelection`, `QEMUShellSelection`, `ContainerShellSelection`, `*PathSelection`, `PathnameConstructor` | `hostctl.host.ShellSelection`, `hostctl.host.PathnameSelection` |
| `ContainerConfig(user=)` (`?user=`); `SerialConfig(port=, protocol=)`; `SerialConsoleProtocol` | `username=` (`?username=`); `device=`, `profile=`; `ConsoleProfile` |
| `WinRMConfig(transport=, ssl=, server_cert_validation=)` (and the same URI keys) | `auth=`, `tls=`, `verify=` (bool, `?verify=false`) |
| `WinRMConfig(read_timeout_sec=, operation_timeout_sec=)` (and the same URI keys; also `read_timeout`, `operation_timeout`) | `timeout=Timeout(connect=10)` (default 30 s, `?timeout_connect=10`) where the aim was to stop a hang while connecting; `timeout=600` (the total, `?timeout=600`) where it was to let a long command finish. The per-request knobs are internal; the old names raise and say so |
| `WinRMConfig(transport="basic"/"plaintext"/"ssl")` over plain HTTP (the password sent in cleartext on pywinrm) | `ValueError` unless `cleartext_auth=True` (`?cleartext_auth=true`); or use `tls=True` |
| native WinRM: `read_timeout_sec=` raised so a long command could finish (it was the command's deadline) | `timeout=600`, the total (default 30 s, `None` for none), the default for `run(timeout=)` on every leg; `timeout=Timeout(connect=...)` bounds opening the session |
| pywinrm and PSRP commands with no deadline; `run(timeout=)` refused | the 30 s total applies; pass `timeout=None` to keep them unbounded; `run(timeout=)` is accepted, and `run(timeout=None)` lifts the total for one call |
| `QemuConfig(agent_timeout=5)` (`?agent_timeout=5`) | `QEMUConfig(timeout=Timeout(read=5))` (`?timeout_read=5`); `timeout=5` alone is the command total, not the agent reply |
| `SerialConfig(read_timeout=0.5, write_timeout=2)` (`?read_timeout=0.5&write_timeout=2`) | `SerialConfig(timeout=Timeout(read=0.5, write=2))` (`?timeout_read=0.5&timeout_write=2`); `read_timeout=0` (non-blocking) has no spelling, since a `Timeout` field is positive; `inter_byte_timeout` is unchanged |
| a config with no timeout of its own (`LocalConfig()`, `SshConfig`, `ContainerConfig`, the system configs) | `timeout=60` on any of them sets the default of `run(timeout=)` (a container refuses it: it has no total); `Timeout(connect=, read=, write=)` where the transport honours them, and `ValueError` where it does not |
| positional config arguments after the first | keyword-only (`TypeError` otherwise) |
| `ExecutorCommand`; `executor.PathLike`, `Input`, `Environment`, `FileHandle` | `CommandLike`; `StrPath`, `CommandInput`, `CommandEnvironment`, `StreamTarget` |
| `pypsrp_available()`, `require_pypsrp()` | `has_psrp()` (and new `has_ssh()`); the guard is internal |
| `host.path(p, backend=name)` re-selecting a provider per operation | pinned to `name`, as `.via(name)`; `host.path(p)` selects per operation |
| `register_system_provider(name, resolver)` with `resolver(config, kind)` returning one provider | `resolver(host)` returning `(executor provider or None, path provider or None)`; the config is `host.config`. One resolver reached by two names runs once per host, so its two legs share a transport. A two-argument resolver raises `TypeError` at registration |
| `SystemConfig(executor=(), path=())` meaning "none" | `executor=None`/`path=None` (the defaults) take the host class's `default_providers()`, which is empty for the built-in hosts; `()` still composes none. `config.executors`/`config.paths` are `None` when not named |
| `host.last_selection`, `ProviderSelection.trace`, `path.selection_trace` entries as dicts (`item["provider"]`, `item["chosen"]`, ..., `item["policy"]`) | frozen `SelectionStep` records (`step.provider`, `step.chosen`, ...); `policy`, which was always `"ordered"`, is gone |
| `host.provider_details` entries as dicts (`item["name"]`, `item["kind"]`, ..., `item["policy"]`) | frozen `ProviderDetail` records; the name is `detail.provider`; `policy` is gone |
| `serial:///dev/ttyUSB0` (any `serial:///NAME`, `serial:/NAME`) naming the relative device `dev/ttyUSB0` / `NAME`; `serial:////dev/x` naming `dev/x` | the absolute `/dev/ttyUSB0` / `/NAME`; a relative device is `serial:NAME`; `serial:////...` raises `ValueError`; `COMn`, `\\.\...`, pyserial URLs and the encoded `serial:///%2Fdev%2Fx` are unchanged |
| `CompositePosixPath.from_path(backend_path, provider, factory, ...)` (undocumented) | `CompositePosixPath.over(providers, *segments, backend=None)`; the same for `CompositeWindowsPath` |
| `SSHConfig(known_hosts=None)`, `HostConfig(uri, known_hosts=None)` (no host-key check) | `SSHConfig(verify=False)`, `?verify=false`; `known_hosts=None` raises `ValueError` |
| an `ssh_config` with `UserKnownHostsFile none` switching the host-key check off | `verify=False` (the config raises `ValueError` at connect without it) |
| `REQUESTS_CA_BUNDLE` to make pywinrm or PSRP trust a private CA | `WinRMConfig(verify="ca.pem")` (`?verify=ca.pem`); the variable still applies under `verify=True` |

pytruenas 0.5.4 and earlier pin `hostctl<0.4` and import the 0.3 paths
(`from hostctl.host import PosixHost, SshConfig, ...`), so they do not import
against 0.4.0; `test.yml`'s informative `downstream-pytruenas` job fails at
collection until pytruenas ports to these names.

### Added

- **`hostctl.deploy`: install a Python program on a host that cannot install
  it itself**, behind a new `deploy` extra (`packaging`, `pathspec`, and
  `tomli` before Python 3.11; included in `dev`). `deploy(host, source,
  path, mode="pyz"|"dir", ...)` probes the target interpreter with a
  stdlib-only script, bundles only the part of the distribution's installed
  dependency closure the target lacks (markers evaluated for the target),
  and installs a reproducible zipapp or tree atomically through `host.run()`
  and `host.path()`; an unchanged digest writes nothing, a path a deploy did
  not write is never replaced, and an interrupted tree swap is restored.
  `bundle()` writes the same layouts locally, `SourceTree` ships a working
  tree filtered by its ignore files, and `Deployment.command` is the argv
  that runs the result. POSIX targets only. A bundle that cannot be built
  raises the new `BundleError` (`HostctlError`, `RuntimeError`), exported
  from `hostctl`. The code and its tests come from pytruenas's generic
  bundler, which knew nothing of TrueNAS.

- **`hostctl deploy URI SOURCE PATH [-- ARG...]`**, the command-line form
  of `hostctl.deploy` (`--mode`, `--package`, `--extra`, `--skip`,
  `--force`, `--python`, and `--tree` with `--ignore-file`,
  `--ignore-pattern`, `--pythonpath`). Arguments after `--` run the
  installed program and its status becomes the command's. Served over MCP
  like every subcommand. A `BundleError` and a step that fails on the host
  (`subprocess.CalledProcessError`, from any subcommand) exit 125 with one
  line, the step's last stderr line included.

- **`hostctl run` and `hostctl info` on several hosts**: more URI operands,
  comma lists and `[1-3]`/`[a-c]` ranges in any operand (outside its user
  part; a later host without a scheme takes the previous one's scheme and
  user), and `--parallel N`. Each `run` output line starts with `[URI] `,
  `info` prints one `{"target": URI, "result": {...}}` line per host, a
  failing host gets a `hostctl: [URI] exit status N` line, and the exit
  status is the worst: 130, then 125-127, then the largest remote status.
  A password is read once for all hosts. One host behaves exactly as
  before, over MCP too (`uri` stays a string; further hosts are `more`).

- **Typed selection records, a host name and an audible fallback**, in
  0.4.0. `SelectionStep` and `ProviderDetail` (in `hostctl` and
  `hostctl.provider`) replace the dicts in traces and `provider_details`.
  `Host.name`/`HostConfig.name` label the target without a credential, and
  `Host.logger` (settable) prefixes it to each record and sets `record.host`.
  When `run()` or a path operation is served by a provider ranked below an
  unavailable one, the host logs one WARNING per skipped provider on
  `host.logger`; it used to be a DEBUG record from the selector only.

- **Host-bound provider descriptors**, in 0.4.0. A `SystemHost` subclass
  declares its own descriptors in `provider_resolvers` (name ->
  `resolver(host)`), merged up the MRO ahead of `register_system_provider`'s
  global ones; mapping a name to `None` hides it. `default_providers()` names
  the descriptors used when the config names none, and drops a default
  `ssh`/`sftp` when the `ssh` extra is missing (a name the config gives is
  kept, and fails at use with the install hint). `resolve_providers()` turns
  names into providers, for `replace_providers()`; an unknown name raises
  `ValueError` listing the names the host knows. `PosixHost(config)` built
  directly now resolves the config's descriptors, which it used to ignore.

- `hostctl.transports.ssh.SSHExecutorProvider` is public, with
  `.connection` (the asyncssh connection `run()` uses, opened on first
  access) and `.loop` (the `hostctl-asyncssh-loop` event loop it is bound
  to; run its coroutines with `asyncio.run_coroutine_threadsafe(coro,
  provider.loop)`), so a caller needing the raw connection no longer reads
  the private transport. This ships in 0.4.0.
- `hostctl.HostctlError` is the base of every exception hostctl raises, in
  the new `hostctl.exceptions` module with `TransportError` and
  `ProtocolError` (both `ConnectionError`s) as categories; every class is
  re-exported from `hostctl`. This ships in 0.4.0.
- `hostctl.has_ssh()` reports whether the `ssh` extra (asyncssh and sftppath)
  is installed, and `has_psrp()` the same for `psrp`, without importing
  either; one table of extras drives both, the install hint, and the SFTP
  provider's availability. This ships in 0.4.0.
- `SshConfig(ssh_config=)` reads one ssh_config on both legs (neither reads one
  by default; it is a keyword only, never a URI key), `SshConfig(connect_options=)` passes extra keywords to
  `asyncssh.connect` on both legs, and `SshConfig(path_flavor="auto")` asks the
  server's login directory while the host is created.
- `hostctl.Timeout(total, *, connect, read, write)` and `hostctl.DEFAULT`:
  every config (local, SSH, WinRM, container, QEMU, serial and the system
  configs) takes `timeout=`, a number or `None` (the total, the default of
  `run(timeout=)`) or a `Timeout`, and stores a `Timeout`.
  `config.timeouts` shows what each `DEFAULT` resolves to; a field the
  transport cannot honour raises `ValueError` naming the config and field.
  The URI keys are `timeout`, `timeout_connect`, `timeout_read` and
  `timeout_write` on every scheme, empty for `None`. This ships in 0.4.0.
- SSH connections are bounded while they establish: `SSHConfig`'s
  `timeout.connect` defaults to 30 s (URI `?timeout_connect=`; `None` keeps
  asyncssh's default).
- `WinRMConfig`'s `timeout.connect` (default 30 s) bounds establishing the
  connection with the same meaning: `-OpenTimeout` on the native leg,
  pypsrp's `connection_timeout` on PSRP, and the connect half of each HTTP
  request's timeout on pywinrm (`None` keeps each library's default). An
  expiry raises `ConnectionError`. This ships in 0.4.0.
- `hostctl.shell.wants_epilogue(capabilities)` answers whether a script for a
  provider needs the flavour's status epilogue (not when it declares
  `manages_status`). `shell_flavour_names()` (also in `hostctl`) lists the
  names `shell_flavour()` accepts, plugin flavours included.
- `run(timeout=)` on every WinRM leg, with the config's total (30 s; URI
  `?timeout=`, empty for `None`) as its default, and
  `RunspaceSession.invoke(timeout=)`. This ships in 0.4.0.
- `pathapi.protocols.supports(path, Protocol)` answers for a host path. A
  composed host's path answers from the operations its providers declare
  (only the pinned provider after `.via()`), without probing; a container path
  has no `Create`, `Chmod`, `Remove` or `Move`, and a QGA path has no
  `Readlink` or `Symlink` and answers its metadata and namespace protocols
  from the guest helper.
- `HOSTCTL_MCP=stdio` serves every `hostctl` subcommand as an MCP tool over
  stdio. `--ask-password` is refused there, since a tool call cannot prompt;
  use `HOSTCTL_PASSWORD`.
- `hostctl --version`, and `-v`/`-q`/`--loglevel` on every subcommand.
- `PromptConsoleProfile(login_timeout=10.0)` sets how long each login step and
  the first prompt may take; the default is the previous fixed 10 seconds. It
  is a profile argument only, not part of the `serial:///` URI.
- `QemuHost.open_serial(encoding=, errors=)` (and `QemuSerialConsole.open`)
  return a console that reads and writes `str`.
- `ContainerConfig(path_flavor="posix")`, `"windows"` and `SshConfig(path_flavor=
  "windows")` take the strings `QemuConfig` already accepted.
- `run(..., stderr=subprocess.STDOUT)` on a QGA guest asks qemu-ga to
  interleave the streams (`capture-output: "merged"`) when the agent is 8.0 or
  later and the guest is not Windows.
- `host_schemes()` and `system_provider_names()` list what the URI-scheme and
  system-provider registries hold, sorted, with entry-point names included and
  not imported. Unknown system-provider descriptors load the
  `hostctl.system_providers` entry point of that name.
- `SystemHost.executor_providers` and `path_providers` (read-only tuples, until
  now only on `LocalHost`) and `SystemHost.replace_providers(*, executor=None,
  path=None)`, which swaps either set, closes each replaced transport once
  unless a kept provider shares it, and starts a new connection generation.
  The provider teardown rule is documented in the providers guide.
- `local_providers()` returns the local `(executor_provider, path_provider)`
  pair, beside `ssh_providers()` and `winrm_providers()`, for a host you
  assemble yourself. `posix://...?executor=local` now builds the same
  providers.
- `ShellFlavour.redirect(value, target, operand)` renders a `REDIRECT`/`APPEND`
  together with its target, for a flavour whose spelling depends on the
  target; the default is `operator(value) + operand`.
- A Connecting guide (one section per transport: URI form, query keys,
  credentials, host-key or TLS policy, extra) and a worked application host
  in "Extending hostctl": a `HostConfig` subclass with its own scheme
  composing a `PosixHost` subclass from SSH providers and an application
  path provider. This ships in 0.4.0.
- WinRM, container and QEMU guest paths set modification times (pathapi's
  `Utime`), so `PathSyncer` carries the source's mtime across and a
  `stat_checksum` sync into them converges instead of re-copying every file.
  WinRM paths and Windows guests use PowerShell (100 ns, a read-only file
  included); a POSIX guest with a path helper uses `touch -c -d @EPOCH`
  (nanoseconds on GNU, whole seconds on busybox); a container path execs
  `touch` as uid 0 or `powershell.exe`, since the archive API has no time
  call. `supports(path, Utime)` is False for a guest without a helper and a
  container without the program. `GuestPathHelper` gains an optional
  `utime(path, *, atime=None, mtime=None)`.
- `CompositePosixPath.over(providers, *segments, backend=None)` and
  `CompositeWindowsPath.over(...)` build a composite path with no host, from
  an iterable of path providers or a `ProviderSelector`. `host.path()` calls
  the same constructor, so per-operation fallback, `.via()` pinning,
  `supports()`, forwarding and `backend=` behave alike. This ships in 0.4.0.
- `hostctl.executor.BufferedExecutor`, `StreamingExecutor` and
  `ExecutorRequest`: base classes for an executor written outside hostctl.
  A transport implements `_exchange(request)`, or `_send`/`_receive` (and
  `_abandon` when it can stop a command), and inherits the subprocess-shaped
  call every built-in executor shares: option and capability refusal,
  `input=`/`stdin=` as bytes, the `text`/`encoding`/`errors` rule,
  capture and stream routing (streamed output written as it arrives),
  `check=True`, the executor's own default for an omitted `timeout`, and a
  `TimeoutExpired` from `expired()` carrying the output captured so far. An
  `OperationNotStartedError` raised after the command was sent becomes a
  `ConnectionError`, so it is never replayed through another provider. The
  container and PSRP executors are built on `BufferedExecutor`. The guide's
  "Writing an executor" section has a worked example. This ships in 0.4.0.
- **One verification switch, `verify=`, on every network config.**
  `WinRMConfig` and `ContainerConfig` take `True`, `False` or a CA bundle
  path (a PEM file or an OpenSSL hashed directory, `str` or `os.PathLike`)
  trusted instead of the transport's default; `SSHConfig` (and so a
  `qga+ssh` tunnel) takes `True` or `False`. The URI key is `verify=`
  (`true`/`false`, or a percent-encoded path), and it round-trips. A bundle
  reaches pywinrm as `ca_trust_path` and pypsrp as `cert_validation`, and
  neither certifi nor `REQUESTS_CA_BUNDLE` replaces it; a missing one raises
  `FileNotFoundError` before connecting. The native WinRM leg trusts only the
  Windows certificate store and refuses a bundle. A container's `verify`
  applies to an `https://` `engine_url` hostctl builds the client for, and is
  pinned against `requests`' `REQUESTS_CA_BUNDLE` override. This ships in
  0.4.0.

### Changed

- **Breaking, 0.4.0: every config spells its timeouts as one
  `timeout=Timeout(...)`.** `QemuConfig(agent_timeout=)` is
  `timeout=Timeout(read=)` and `SerialConfig(read_timeout=, write_timeout=)`
  is `timeout=Timeout(read=, write=)`; their URI keys
  (`?agent_timeout=`, `?read_timeout=`, `?write_timeout=`) raise naming
  `timeout_read`/`timeout_write`. A number given as `timeout=` is the total
  only. `run(timeout=None)` is no limit for that call on every host, and
  `host.shell.run(timeout=None)` passes it on; a left-out `timeout` is the
  config's total. A QEMU guest-agent reply may now be unbounded
  (`Timeout(read=None)`); serial's non-blocking `read_timeout=0` is refused.
  See "Migrating from 0.3".

- **Breaking, 0.4.0: `WinRMConfig` honours two timeouts, `total` and
  `connect`.** The total, 30 s, is the default for `run(timeout=)` on every
  WinRM leg. The native leg used `read_timeout_sec` as each command's total
  deadline; it now uses the total, and `timeout.connect` (default 30 s)
  bounds opening the remote session (`New-PSSessionOption -OpenTimeout`).
  pywinrm and PSRP had no deadline and refused `run(timeout=)`; both now
  apply the total too, so a command running past 30 s raises
  `TimeoutExpired` (`orphaned=True`, after asking the server to stop it)
  where it used to finish. Pass `timeout=None` for no default, or
  `run(timeout=)` per call. The per-request WS-Man timing
  (`read_timeout_sec`, `operation_timeout_sec`) is internal: a 20 s server
  hold and a 30 s HTTP read limit. See "Migrating from 0.3".

- **Breaking, 0.4.0: `host.path(p, backend=name)` pins the path to that
  provider**, as `.via(name)` does: every operation on it and on every path
  derived from it goes to `name`. Before, the named provider only built the
  path and each operation re-selected from the first capable provider, so a
  write meant for `name` could land on another one.

- **Logger names follow module paths under `hostctl`**, which ships in
  0.4.0: every module logs through `logging.getLogger(__name__)`
  (`hostctl.transports.ssh._transport`, `hostctl.provider._contracts`, ...).
  Configure the `hostctl` logger; the child names are not API.

- **Breaking, 0.4.0: `OperationNotStartedError` is a `ConnectionError`, not
  a `RuntimeError`.** It is raised only before anything was sent, so an
  `except ConnectionError` retry around a refused SSH port now catches it.
  An `except RuntimeError` written for it no longer does. The QGA errors
  are renamed (`QGA*`), `GuestAgentProtocolError` is folded into
  `QGAProtocolError`, and the exceptions leave `hostctl.provider`,
  `hostctl.executor` and `hostctl.serial` for `hostctl.exceptions`; see
  "Migrating from 0.3".
- **Breaking, 0.4.0: one keyword vocabulary.** Every config takes `shell=`
  and `pathname=` (`hostctl.host.ShellSelection`/`PathnameSelection`), its
  target positionally and everything else by keyword, and the URI query keys
  follow the keywords; an old key raises `unknown connection parameter`. WinRM
  takes `auth=`, `tls=`, `verify=True|False` and float-second timeouts, the
  container `username=`, the serial config `device=` and `profile=`. See
  "Migrating from 0.3".
- **Breaking, 0.4.0: module paths below `hostctl` moved.** Each transport is
  one package under `hostctl.transports`, the contracts are `hostctl.host`,
  `hostctl.executor`, `hostctl.process`, `hostctl.provider`, `hostctl.system`,
  `hostctl.paths` and `hostctl.uri`, and every public module declares
  `__all__`; every other module starts with an underscore. The root
  `__all__` is unchanged apart from the rows in "Migrating from 0.3".
- A Windows QGA guest's path helper starts `powershell.exe -NoProfile
  -NonInteractive -Command -` and sends the script as the call's `input-data`,
  instead of a base64 `-EncodedCommand` argument. Endpoint-protection
  heuristics scan the command line, and a base64 PowerShell launch is what
  they flag; the script now never appears there. Results are unchanged.
- **Breaking, 0.4.0**: the exec leg of an SSH host no longer reads
  `~/.ssh/config`. It did, by asyncssh's default, while the SFTP leg did not,
  so an alias reached the server through `run()` and failed through `path()`.
  Neither leg reads one now; pass the host, key, known_hosts file or proxy
  you took from it to `SshConfig`.
- **Breaking, 0.4.0**: `SshConfig(username=)` defaults to `None`, the local
  account, as `ssh` does, instead of `"root"`; pass `username="root"` to keep
  the old behaviour. `ssh://host` carries no user, and `connection_uri` omits
  `root@`.
- **Breaking, 0.4.0**: `connect()` and `with host:` raise `OperationNotStarted`
  when providers were offered and none connected (a refused port, a DNS
  failure, an unknown host key or a bad key), instead of returning with
  nothing connected.
- **Breaking, 0.4.0**: a relative `host.path()` on an SSH host names the
  login directory, as `run()`'s working directory does, instead of the
  filesystem root; `host.path()` with no segments is the login directory. The
  directory is read once per connection, so building a relative path connects;
  when that connection cannot be opened the SFTP provider declines
  (`OperationNotStartedError`) and the host builds the path from its next
  path provider.
- A host used only for paths no longer also opens and authenticates the SSH
  exec connection; each connection authenticates once.
- The PowerShell dialect over SSH sends `-Command` where the login shell
  allows it, a script with a `#` comment on the channel's stdin when the call
  has no input of its own, and `-EncodedCommand` only as the last resort.
  `ShellCommand` gains an optional `stdin`.
- **Breaking, 0.4.0**: `ShellFlavour.command()` takes `login_shell=None`; a
  custom flavour must accept it to be used over SSH. The POSIX `command()`
  puts `cwd` inside the script instead of a leading `cd ... &&`, and joins
  with a quoting that sh-family shells and fish read identically.
- The config extension hooks `_from_parsed_uri` and `_create_host` are documented
  as the override points for a `HostConfig` subclass, with a worked example in
  the extending guide. Their names are unchanged and no `from_uri` is added.
- **Breaking, 0.4.0: an environment variable name outside
  `[A-Za-z_][A-Za-z0-9_]*` raises `ValueError` on every transport**, before a
  provider is selected. Only the embedding transports refused `A.B` or
  `ProgramFiles(x86)` before, so the same call was valid or not depending on
  which provider won. `validate_environment_names` is exported by
  `hostctl.executor`.
- **Breaking, 0.4.0: `Input` no longer includes `int`.** `input=3` reached
  `subprocess`' writer thread and hung until `timeout` on Python 3.14; it now
  raises `TypeError` at once.
- **Breaking, 0.4.0: `Shell` is not an `Executor`.** `Shell.execute()` and
  `shell(command, *args)` are removed; calling a `Shell` configures it
  (`host.shell(cwd=..., env=...)`) and a positional argument raises
  `TypeError`. Run a program with argv through `host.executor(command, *args)`
  or `host.run(Exec(command, *args))`. A shell's `flavour`, `cwd`, `env`,
  `encoding` and `errors` are read-only, and the `host.shell` proxy is gone.
  A `Shell` over a host is that host's `run`; one over a bare executor sends
  the flavour's invocation as argv when the executor declares `args`.
- **Breaking, 0.4.0: `ShellFlavour.script(for_session=)` is
  `script(epilogue=)`**, with the sense inverted (`for_session=True` is
  `epilogue=False`). `terminate_submission` is unchanged here.
- **Breaking, 0.4.0: a structured `cmd` `call` raises `ValueError`.** `call`
  re-expands `%` and doubles `^` in its arguments, so no quoting survives it;
  run the program directly. `call` and `start` are no longer treated as
  builtins.
- **Breaking, 0.4.0: `Cmdlet` and `PSParam` replace the PowerShell `-Name`
  inference.** A plain string in a structured command is always a value; a
  string shaped like `-LiteralPath` was rendered bare, which hid a real
  argument from a native program and let a program-shaped value read as a
  parameter. Write `Cmdlet("Remove-Item", PSParam("-LiteralPath"), path)`.
  Both are exported by `hostctl`, `hostctl.host` and `hostctl.shell`, are
  non-iterable like `Exec`, and raise `TypeError` for any other shell.
  `ShellTarget` gains `BATCH` and `UNRESOLVED`.
- **The `hostctl` command needs the `cli` extra** (`pip install
  "hostctl[cli]"`, which adds `duho>=0.7.5,<0.8`). Without it the console
  script prints one line naming the extra and exits non-zero. The command
  line moved from `hostctl._cli` to `hostctl.cli`; `hostctl.cli.main(argv)`
  no longer takes `stdout=`/`stderr=` and writes to `sys.stdout`/`sys.stderr`.
  Subcommands, operands and exit statuses are unchanged, except that the
  `--` before `run`'s program is now required.
- **Paths are `pathapi` paths** (`pathapi>=0.1.0,<0.2`, which replaces
  `pathlib_next`; `pathlib-next` was renamed `pathapi`). `HostPath` is
  `pathapi.Path`, `ExecutorCommand` takes a `pathapi.Pathname`, and a path
  provider must return a `pathapi.Path`; one that returns anything else fails
  with `TypeError: path provider 'X' returned LocalPath, expected
  pathapi.Path`. `local_providers()` is the local pair to use instead of
  building one. Change `pathlib_next` imports as
  pathapi's changelog says: `pathlib_next.utils.sync` is `pathapi.sync`,
  `pathlib_next.utils.stat.FileStat` is `pathapi.FileStat`.
  - The `ssh` extra is `asyncssh>=2.20,<3` and `sftppath[asyncssh]>=0.1.1,<0.2`,
    in place of `pathlib_next[sftp-async]`. An SSH host's path is an
    `sftppath.SFTPPath`, and its `str()` is the path on the server where it
    was `sftp://host:22/...`.
  - pathapi's own changes reach hostctl's paths: `touch(mode=...)` raises
    `NotImplementedError` on a path that cannot `chmod()`, and a path class
    without `__fspath__` is no longer `os.PathLike`.
  - A WinRM, Docker archive or QGA write stream whose `with` block raises
    still uploads nothing, and no longer also emits a `ResourceWarning`; one
    collected unclosed still warns. The stream is pathapi's `DiscardOnError`.
  - `pathlib.Path(src).copy(host_path)` onto an existing directory of a
    WinRM, Docker archive or QGA host raises `FileExistsError`, as onto a local
    directory; it merged before.
  - A WinRM, Docker archive or QGA write stream whose upload fails on
    `close()` is closed; write again to retry.
- **Requires netimps 0.4** (`netimps>=0.4.0,<0.5`, was `>=0.3.0,<0.4`). No
  hostctl code changed, but three things change for a caller:
  - `winrm`, `winrms`, `wsman`, `wsmans` and `psrp` now have default ports
    (5985, 5986, 5985, 5986, 5985) where `ConnectionString.port` was `None`,
    so `str(ConnectionString("winrm://admin@host"))` renders
    `winrm://admin@host:5985`. They are the ports `WinRMConfig` already chose.
  - An all-digit scheme given through `scheme=`, `defaults=` or `replace()` is
    read as a port, and one outside 0-65535 raises `ValueError` where it gave
    an object with no port.
  - netimps 0.4 refuses to import when `NETIMPS_NO_SOCKET_PATCH` is set; set
    `NETIMPS_SOCKET_PATCH=0` instead. `import hostctl` does not import
    netimps and changes no stdlib module: only the first call that needs it
    (a default port, a bracketed IPv6 authority, `ConnectionString`) imports
    it, and with it netimps's `socket` patch on Windows.
- The sdist no longer ships the contributor `AGENTS.md` or `benchmarks/`, and
  neither artifact ships a `CLAUDE*` file.
- Extras ranges follow the dependency rule: `container` accepts docker 7.0
  (was 7.1), `serial` requires pyserial 3.3 (was 3.5), `psrp` is limited to
  pypsrp 0.9.x (was `<1`), and `qemu-libvirt` accepts libvirt-python 12 and
  later (was `<12`).
- **The pywinrm leg no longer loads the remote user's PowerShell profile and
  runs non-interactive**, as the native leg already did. It starts
  `powershell.exe` directly and sends the script on stdin, instead of through
  cmd.exe. This ships in 0.4.0.
- **`provider="pywinrm"` always means pywinrm**, which ships in 0.4.0. It used
  to mean the native current-context leg for a password-free config on Windows;
  use the new `provider="native"` (or `"auto"`) for that. An explicit
  `"pywinrm"` or `"psrp"` without a password now needs `transport="kerberos"`.
  `str(WinRMConfig)` omits an unset port, so `HostConfig(str(config)) == config`.
- **One status rule on every WinRM leg**, which ships in 0.4.0: 0 when the last
  statement succeeded, else the native exit code, else 1. PSRP reports a
  non-terminating error followed by a successful statement as 0, where it was
  1, and a remote ACL `PermissionDenied` is a status rather than
  `PermissionError`. Only transport and authentication failures raise.
- **PSRP `run()` text matches the other legs** (tables as a console shows
  them, CRLF line ends), which ships in 0.4.0. It was each object's
  `ToString()` joined with LF.
- **`SystemConfig` (`PosixConfig`, `WindowsConfig`, `IosConfig`) takes only
  `provider_options=` and `initializer=` beyond `shell=`, `executor=` and
  `path=`**, which ships in 0.4.0. Any other keyword was kept in `options`
  without complaint, so `executr=("local",)` configured nothing; it now raises
  `TypeError`. `executor="local"` (a bare string) raises `TypeError` where it
  became five one-letter descriptors. A `SystemHost` built from a connection
  string or anything but a `HostConfig` raises `TypeError`.
- **`ProviderSelector.select()` loses `policy=`, `ProviderSelection` loses
  `policy`, and `ProviderSelector.last_selection` is gone**, which ships in
  0.4.0. Nothing varied the policy and nothing but one test read
  `last_selection`; `host.last_selection` (the trace) stays, and trace entries
  keep a constant `"policy": "ordered"`.
- **Registries refuse a duplicate name**, which ships in 0.4.0. A second
  `HostConfig` subclass claiming a scheme that another live config holds
  raises `ValueError` where the class is defined (it used to fail later as
  "ambiguous host URI"); `register_system_provider(name, resolver)` raises for
  a name already registered. Pass `replace=True` to take the name over on
  purpose. Redefining the same class (a module reload) is not a duplicate.
- Composite paths are on one filesystem (`_same_filesystem()`, which guards
  `copy()`, `move()` and `PathSyncer` against a file copied onto itself) when
  they can route to the same provider object, not only when they came from
  one host's provider set. `pickle` and `copy.deepcopy()` of a composite raise
  `TypeError` naming the reason, rather than whichever error the first
  unpicklable provider attribute produced. This ships in 0.4.0.
- **`SSHConfig(verify=False)` is the one way to skip the host-key check.**
  `known_hosts=None` raises `ValueError` naming it, `SSHKnownHosts` no longer
  admits `None`, and an `ssh_config` whose `UserKnownHostsFile none` would
  switch the check off raises at connect unless `verify=False`. A
  `known_hosts` the caller names is checked even under `verify=False`.
  **Breaking, 0.4.0.**
- **`WinRMConfig.verify` is `bool | str | os.PathLike`**, where it was a
  bool: a string is a CA bundle path, needs `tls=True`, and one spelling a
  boolean (`"false"`) raises `ValueError`. Native WinRM's `verify=False` adds
  `-SkipRevocationCheck` to `-SkipCACheck -SkipCNCheck`, so it checks nothing,
  as the other legs do. This ships in 0.4.0.

### Removed

- **Breaking, 0.4.0**: `ShellFlavour.command_path()`, which nothing called.
- **Breaking, 0.4.0**: `ShellFlavour.terminate_submission()` and
  `submission_terminators`. `ShellSession.send()` writes the script and the
  line terminator and nothing else.
- **`PosixHost.from_ssh(config)` and `WindowsHost.from_winrm(config)`**, which
  ship removed in 0.4.0. Each was `config._create_host()` plus a check that
  could not fail for a config that picked its own paths. Use `config.open()`
  or `Host(uri)`; `SshConfig("host").open()` is a `PosixHost`, and a config with
  Windows paths opens a `WindowsHost`.
- **Breaking, 0.4.0**: the undocumented `CompositePosixPath.from_path()` and
  `CompositeWindowsPath.from_path()`. Use `over(providers, *segments,
  backend=name)`.

### Fixed

- A PSRP `run(..., text=True)` that times out carries `""` in
  `TimeoutExpired.output`/`.stderr`, the call's output type, as the local
  executor does; it carried `b""`.
- Reading a directory as a file over SSH raises `IsADirectoryError` against
  OpenSSH too (sftppath 0.1.1, now the floor of the `ssh` extra); it returned
  `b""` from a Windows server and a bare `OSError("Failure")` from Linux.
- `SSHConfig(timeout=Timeout(connect=None))` round-trips through
  `connection_uri` (an empty `timeout_connect=`); the URI it rendered could
  not be parsed.
- On Windows, a process without a console no longer opens a console window
  for every local command: the local executor starts children with
  `CREATE_NO_WINDOW` unless the caller passes `creationflags=`, and so does
  the native WinRM leg for its local `powershell.exe` relay.
- WinRM, container and QEMU paths keep their backend through `with_name()`,
  `with_stem()`, `with_suffix()`, `relative_to()` and `parents` on Python 3.9 to
  3.11, where pathlib built them without one; an overwriting copy onto such a
  path failed with `AttributeError: _backend` under pathapi 0.1.1.
- Closing a container process releases the exec socket's descriptor; it
  stayed open behind the response and socket file docker-py keeps on it.
- A shell flavour's `repr()` names it (`<PosixShellFlavour 'posix'>`), so a
  config's `repr()` no longer embeds `<... object at 0x...>`. This ships in
  0.4.0.
- Config and host constructors report their real parameters to type
  checkers, `help()` and `inspect.signature()`: `SshConfig("h")` is an
  `SshConfig` to a checker, which flags a misspelled keyword, and
  `HostConfig(uri)`/`Host(uri)` show `(connection_string, /, **secrets)`.
  This ships in 0.4.0.
- `hostctl ls` escapes control characters, backslashes and undecodable bytes
  in names, so a name can no longer forge listing lines or send terminal
  escape sequences, and an undecodable name no longer raises.
- `hostctl run`/`ls`/`cp` on an unreachable host exit 125 with one line
  instead of a traceback and status 1. `hostctl info` exits 125 (126 on an
  authentication refusal) instead of printing an identity made up from the
  URI with status 0. `hostctl --help` no longer lists the unregistered
  `psrp://` scheme; PSRP is `winrm://...?provider=psrp`.
- `hostctl cp` no longer splits `ssh://user:password@host` at the password's
  colon, and no operand error echoes a password. `HOSTCTL_PASSWORD` no
  longer makes every `qemu+libvirt:`/`qga+unix:` command exit 125.
- A URI whose password holds an unencoded `/`, `?` or `#` no longer has part
  of the password echoed in the `ValueError` from `HostConfig(uri)`,
  `Host(uri)` or `ConnectionString(uri)`; the message now says to
  percent-encode it. Only an authority that does not parse is refused: a URI
  with a valid port and an `@` later (`ssh://nas:22/mail/user@example.com`,
  `ssh://root:123/rest@nas`) is taken at face value, as in 0.3.
- Values passed in `env=` no longer appear in `CalledProcessError` or
  `TimeoutExpired` text, `repr()`, `CompletedProcess.args` or the DEBUG
  dispatch log on transports that embed them in the command. Each shows as
  `<redacted>`; the value still reaches the remote command line. fish
  `set -gx` and quoted values containing `'` are also redacted in raw
  command text.
- PowerShell-dialect stderr over SSH is text, not CLIXML, and a long
  PowerShell script is no longer refused by endpoint protection that flags
  `-EncodedCommand`.
- A missing `sftppath` no longer surfaces as a bare `ModuleNotFoundError`: the
  SFTP provider probes `unavailable` and `path()` raises the `ssh`-extra
  `ImportError`. The libvirt QGA message names `pip install
  hostctl[qemu-libvirt]`.
- `dialect="auto"` over SSH accepts ash (BusyBox), ksh, mksh and the other
  POSIX shells it used to refuse; csh and tcsh still raise, now saying they are
  unsupported.
- `ssh://h:0` raises the port `ValueError` instead of silently using 22.
- `SshProcess.wait(timeout=)` no longer loses output that arrived before the
  timeout; `read()` returns it afterwards.
- An SSH host or username that OpenSSH would refuse now raises `ValueError`
  at construction; such a host could inject options into a ProxyCommand.
- `spawn()` over SSH escapes the command line for a `cmd` login shell, as
  `run()` does; an argument such as `a&b` ran as a second command (command
  injection).
- A refused SSH exec request (the server declined to start the command) is an
  `OSError` rather than a `ConnectionError`, so it no longer reads as a broken
  connection.
- `close()` of an SSH host in a forked child no longer closes the parent's
  connection.
- A relative path on an SSH host is no longer silently rooted at `/` (or
  `C:\`) while `run()` starts in the login directory.
- A fish login shell over SSH reads structured arguments verbatim; a value
  ending in a backslash could end the quoting early and run what followed
  (command injection).
- `run(timeout=)` over SSH returns within about three seconds of the deadline
  even when the server or command ignores TERM, and `TimeoutExpired.orphaned`
  is true unless the command was seen to end.
- **Output stays in order around a dispatched command.** The binary fallback
  of `dispatch_output` wrote past text still pending in `sys.stdout`, so a
  command's output could print before the line written before it.
- **A local `timeout=` no longer detaches the terminal.** A child started
  with a timeout got a new session and could not open `/dev/tty`; the new
  session is now used only when this process has no controlling terminal.
  A timed-out command started from a terminal may therefore leave
  grandchildren running (see the divergence ledger).
- A structured `cmd` `echo` prints its operand verbatim: `("echo", "off")`
  switched echo off, `("echo", "/?")` printed help and `("echo",)` printed
  the echo state.
- **A relative `cwd` means the process's directory on POSIX and fish.** `cd --
  sub` consulted `CDPATH` first, so with `CDPATH` set the payload ran in
  another tree with status 0. It is rendered `cd -- ./sub`, which fails when
  the directory is absent.
- **A here-document or open pipeline in a session or script no longer
  swallows the rest.** `send()` turned the delimiter `EOF` into `EOF;`, and
  `script()` joined `EOF;echo next`; after multi-line raw source the separator
  is now a newline (`ShellFlavour.multiline_source`), and a fish session no
  longer sends `echo a |;`.
- fish quotes a word containing `%`; a bare `%self` became the shell's PID.
- **`host.shell.run()` reports the native-WinRM status.** It rendered the
  PowerShell exit epilogue itself, which ended the remote runspace before the
  status marker; it now hands the provider what `host.run()` does. A `Shell`
  over `LocalExecutor` (or any executor declaring `args`) runs, where it
  raised `FileNotFoundError`, and `host.shell(env=...)` reaches the command.
  `SystemHost.executor(command, *args)` raised `NameError`; it runs.
- **PowerShell keeps deferred object output.** The status epilogue ended with
  `exit`, which discards objects still waiting to be formatted:
  `host.run("[pscustomobject]@{a=1}")` returned an empty line with status 0 on
  Windows PowerShell and pwsh. It now ends with `$host.SetShouldExit(...)`;
  the exit status is unchanged. The WinRS (`pywinrm`) path shares the
  epilogue and was not exercised.
- **A `cmd` redirect to a path with a space writes that file.** The operand
  after `REDIRECT`/`APPEND` was quoted for a child's C runtime and wrote a file
  named after its first word; it is now quoted for the shell that reads it.
- **A path used as a whole command runs under PowerShell.** It rendered as a
  string literal that PowerShell printed, with exit status 0; a standalone path
  is now a one-element command (`& 'path'`), and cmd quotes a spaced one.
- A structured `cmd` `start` always gets an empty window title, so
  `("start", "/b", path)` starts the program instead of titling a window.
- A local `Exec` of a `.bat`/`.cmd` in a directory whose path has a space no
  longer fails with `FileNotFoundError`, and a `cmd` structured command whose
  program path has a space starts instead of reporting `'"C:\Program' is not
  recognized`. The first element of a structured command is the program and
  takes the quoting of a command word, not of argv data.
- `iterdir()` of a regular file on a POSIX QGA guest raises `NotADirectoryError`
  instead of listing nothing: the guest helper reads `find`'s record for the
  starting point, and follows it with `-H` when it is a link.
- **`move()` between two WinRM hosts, or two QGA hosts, copies and deletes**
  instead of raising `ValueError`; a direct `rename()` across hosts raises
  `OSError(EXDEV)`. `chmod("755")` works on WinRM and QGA paths.
- **`Path.copy()` into a WinRM, container or QGA path refuses a source name that
  is not one path component** (`ValueError`), where `..\x` could land outside
  the destination.
- **`hostctl cp` keeps a transport's native copy** instead of bypassing the
  source path's own `copy()`.
- **A serial or QEMU console process can no longer free a lease it does not
  hold.** Closing a stale process after the host was closed and reopened
  released the new process's lease, so a third `open()` succeeded and read the
  second process's bytes. A release is now bound to its own acquisition, and
  closing the host (or the executor) ends a live process: its reads and writes
  raise `ConnectionError` and its `close()` is a no-op.
- **Text-mode serial reads no longer corrupt a character split across reads.**
  An idle line reads empty, which is not EOF; the decoder was finalized on it,
  so a split UTF-8 character raised `UnicodeDecodeError` or became two
  U+FFFD. The text read now returns `""` and keeps the partial character for
  the next read.
- `typing.get_type_hints()` works on every public function and method on
  Python 3.9 (18 raised there, 7 on 3.14), and `SystemHost.run` carries the
  same annotations as `Host.run`.
- **QGA writes work on Windows guests again.** A write went through a
  temporary file and a replacing rename that the Windows helper refuses, so
  every write failed since the helper probe landed in 0.3.0. A QGA write is now
  in place (not atomic), which also keeps a POSIX file's inode, mode, owner and
  symlink; only `open("x")` writes a temporary file and renames it.
- A POSIX QGA `rename()` onto an existing target replaces it, as `os.rename`
  does, and a skipped `mv -n` no longer reports success without moving;
  a Windows guest raises `FileExistsError`.
- `iterdir()` of a dash-leading relative path (`-delete`) no longer runs
  `find` with it as an expression: the starting point is anchored with `./`.
- QGA `exists()` below a regular file returns `False`; a guest-file-read reply
  longer than requested and a deeply nested reply raise `QgaProtocolError`; a
  listing skips names that are not one path component; a helper-less QGA path
  provider declares `exists`.
- Container overwrites keep the file's owner, group and mode instead of
  re-owning it to root; `exists()` and `open("x"/"a"/"r+")` read one archive
  header instead of downloading the whole subtree, and an engine error is no
  longer reported as `FileExistsError`; `write_bytes` `PermissionError` carries
  `errno` and `filename`.
- A container path taken before `ContainerHost.close()` raises instead of
  reopening docker-py's client, and a Windows container follows relative links
  with Windows path syntax.
- Two threads on a fresh PSRP host opened two pools, and one was never
  closed.
- A whole-file WinRM read is one round trip (it was one per 8 KiB on Python
  3.9-3.13), and `open("x")` on an existing file no longer downloads it.
- A WinRM rename onto an existing entry raises `FileExistsError`, removing a
  non-empty directory raises `OSError` with `ENOTEMPTY`, and `scandir` of a file
  and a failed reparse point carry an errno, as the local filesystem's do.
- `transport="ssl"` over PSRP raised at the first send; it is now Basic over
  HTTPS, as on pywinrm.
- `RunspaceSession.invoke(script, *args)` rendered `-LiteralPath` as a quoted
  string, which PowerShell's binder rejects; it uses the flavour's
  `structured_command`.
- Writes to a WinRM path over pywinrm work again; every write raised
  `OSError`. `run()` commands over about 3 KB no longer fail with "The command
  line is too long".
- A native (password-free) WinRM command that failed remotely raised
  `ConnectionError` instead of returning its status, so `check=False` could
  not inspect it; a command printing about 20,000 lines hit the deadline
  because the relay was quadratic.
- `host.executor(program, *args)` on a composed or local host raised
  `NameError`; both executor wrappers are now one class.
- A composed host's path now forwards `_utime`, `hardlink_to`, `owner`,
  `group`, `resolve` and a relative `absolute()` to its backend, which
  `PathSyncer` needs to carry mtimes across: a second `stat_checksum` sync into
  an SSH host copies nothing instead of everything. `supports()` answers
  `Utime`, `Hardlink`, `Owned`, `Resolve`, `NativeChecksum` and `Chown` from
  the backend path class, so it no longer claims a digest `LocalPath` lacks or
  a `chown` a WinRM or container path cannot do. `PathProvider` declares
  `utime` and `hardlink_to` by default.
- Concurrent `run()` and `close()` on one host no longer raise `RuntimeError`
  ("dictionary changed size during iteration") from provider selection, which
  read and wrote its decline and probe maps without its lock. Two hosts opened
  concurrently from one config no longer share a transport, or split one host's
  executor and path legs across two.
- `ConnectionString("nas:8443")` and `ConnectionString("10.1.1.1:22")` with no
  scheme raise "no scheme" instead of reading `nas` as the scheme, which left
  the host empty and `is_local` true for a remote machine (Python 3.9 did the
  same for the address form). With `scheme=` or `defaults=` they parse as host
  and port. The `ConnectionString` docstring and the API header said a bare host
  parses without a scheme; they now say it needs one.
- `SshConfig`, `WinRMConfig`, `ContainerConfig` and the system configs refuse
  a URI given as their host with a message that redacts its password;
  `WinRMConfig("winrm://u:pw@h", "u")` used to render the password in
  `connection_uri` and `repr`. A percent-encoded C1 control character
  (`%C2%9B`) in a system URI's host or userinfo is refused like the C0 ones,
  instead of reaching `authority` and `info().hostname` decoded.
- A provider's probe narrows its declared capabilities in every reader.
  `select(capability=)`, `host.capabilities`, `executor_capabilities`,
  `provider_details` and a composed path's dispatch each read a different
  union of the two, so a probe that narrowed `spawn` away was still chosen for
  it and reported. `ProviderSelector.effective_capabilities(provider)` is the
  one answer: the declared set intersected with what the probe states, or the
  declared set when it states none; a capability a probe adds is ignored.
- `host.path(backend=name)` probes through the selector, as `.via(name)`
  does, so a probe that raises is `OperationNotStarted` and is cached, where a
  raw transport error escaped and the probe re-ran on every call.
- `host_checksum` on a Windows SSH target hands `certutil` and `Get-FileHash`
  the file the transfer wrote, as `C:\data\file.txt`: `certutil` read the SFTP
  spelling `/C:/data/file.txt` as an option, and PowerShell hashed the logical
  text. A file on this machine's filesystem is hashed in process whichever host
  owns it; a composed local host (`posix://?executor=local&path=local`)
  spawned one process per file.
- One broken `hostctl.configs` plugin no longer breaks every other unclaimed
  scheme: the entry point named for the scheme loads first, a plugin that fails
  to import is warned about and skipped, and only its own scheme raises
  `RuntimeError` from the import error.
- Default ports, IPv6 bracketing and the local-host check come from netimps
  where hostctl wrote its own: an SSH, QGA-over-SSH, WinRM or PSRP port, an
  endpoint or URI authority with a port, `HostInfo.hostname` (a bare IPv6
  address `::1` is no longer reported as `:`), and `ConnectionString.is_local`,
  which now also answers `*.localhost`, short IPv4 spellings and this machine's
  own name. `import hostctl` still does not import netimps.
- Windows PowerShell 5 writes a `REDIRECT`/`APPEND` to a path or one-element
  tuple with `| Out-File -LiteralPath NAME -Encoding Unicode`, so `[ab]` in the
  name is no longer a wildcard: the write failed, or went to a file the pattern
  matched. The bytes are those `>` wrote (UTF-16LE with a byte-order mark).
  Raw text after the operator, and pwsh, keep `>`.
- Windows PowerShell 5 runs the program an absolute path names when the path
  contains `[`, `]`, `*` or `?`: it is backtick-escaped after `&`, where
  `C:\x\[ab]\t.cmd` ran `C:\x\a\t.cmd` if that existed. A relative path cannot
  be spelled literally on 5; see the divergence ledger.
- Captured pwsh 7 output is plain text: `PWSH` scripts set
  `$PSStyle.OutputRendering = 'PlainText'`, where a `TERM` naming a colour
  terminal put ANSI colour codes into formatted tables and error records even
  through a pipe (`run(..., capture_output=True)` returned
  `'\x1b[32;1ma\x1b[0m'` for `a`).
- A `serial:` URI reads its device the way a `file:` URI reads its path:
  `serial:///dev/ttyUSB0` and `serial:/dev/ttyUSB0` open `/dev/ttyUSB0`, where
  they opened `dev/ttyUSB0` relative to the working directory (the CLI help's
  own example did). A Windows port and a pyserial URL keep no slash
  (`serial:///COM3` is `COM3`, `serial:///loop://` is `loop://`), the encoded
  `serial:///%2Fdev%2FttyUSB0` is still `/dev/ttyUSB0`, `serial:ttyUSB0` is a
  relative device, and `serial:////dev/ttyUSB0` raises `ValueError` naming the
  three-slash spelling. `str(SerialConfig("/dev/ttyUSB0"))` renders
  `serial:///dev/ttyUSB0`, percent-encoding only what a URI path cannot hold.
  This ships in 0.4.0.
- `LocalConfig` and the system configs (`PosixConfig`, `WindowsConfig`,
  `IOSConfig`, `SystemConfig`) print as values, in the dataclass configs'
  style: `LocalConfig()`, `PosixConfig(authority='nas.example', shell=None,
  executor=('ssh',), path=('sftp',))`, where they printed
  `<... object at 0x...>`. `provider_options` and `initializer` are left out,
  so a transport password passed there never reaches a log. This ships in
  0.4.0.

### Security

- **Windows PowerShell 5 no longer splits a native argument at a space that
  follows a quote.** `host.run(("tool.exe", 'report" /MIR'))` reached the
  child as `report"` and `/MIR`, an argument turned into a flag. PS 5 wraps an
  argument in quotes only when whitespace sits at an even count of `"`; a
  value that cannot be passed that way now raises `ValueError` (use `PWSH` or
  `Exec`) instead of arriving split.
- **A batch file never executes an argument.** `Exec(shim_cmd, 'x"&echo ...')`,
  a structured command to a `.bat`/`.cmd` under `cmd`, `powershell` or `pwsh`,
  and a bare name that PATHEXT resolves to a batch file all let a value close
  cmd's quoting and run the rest (and expanded `%VAR%`). Every argument to a
  batch target is now one inert token (`"` + body + `"`, `"` as `""`, `%` as
  `%%cd:~,%`). A Microsoft C runtime reads `""` as one quote; a
  `CommandLineToArgvW` consumer may split a value that has whitespace after a
  quote.
  - **Breaking, 0.4.0**: an argument to a program of unknown type (a bare
    name, `.ps1`) raises `ValueError` when it holds `"` (cmd) or any of
    `" & | < >` (PowerShell), since the program may be a batch file. Name the
    program with its suffix, use `Cmdlet` for a cmdlet, or use `Exec`.
  - **Breaking, 0.4.0**: a local `Exec` of a batch file raises `ValueError`
    when the program path contains `"` or `%`.
- **`WinRMConfig` refuses `transport` `basic`, `plaintext` or `ssl` over plain
  HTTP** unless `cleartext_auth=True` (URI query `cleartext_auth=true`), on
  both the pywinrm and PSRP legs. They sent the password in cleartext on
  pywinrm. This ships in 0.4.0.
- **A PowerShell native argument `--%` raises `ValueError`.** PowerShell reads
  it as the stop-parsing token however it is quoted, so the rest of the line
  reached the program unparsed and re-split. **Breaking, 0.4.0.**
- **No check is off unless the config says `verify=False`.** On SSH,
  `known_hosts=None` and an `ssh_config`'s `UserKnownHostsFile none` each
  switched host-key verification off without it; both now raise. A
  container's CA bundle holds whatever `REQUESTS_CA_BUNDLE` says: stock
  docker-py lets the variable replace a bundle on every request, widening
  what it trusts. This ships in 0.4.0.

## [0.3.2] - 2026-09-23

### Fixed

- **`redact_uri` no longer over-redacts a well-formed URI.** The textual
  fallback -- which reads the last `@` in the whole string as the userinfo
  delimiter -- ran whenever `urlsplit` reported no password, including for
  URIs that simply do not have one. `ssh://nas:22/mail/user@example.com`
  carries no credential at all and came back as `ssh://nas@example.com`: a
  diagnostic quietly naming a different host. Guessing is now confined to
  input that does not parse -- when the URI is well formed, `urlsplit`'s
  authority is the authority, so an `@` in a path is left alone. A password
  hidden by an unencoded `/`, `?` or `#` is still redacted: that input is
  malformed (the separator ends the authority, leaving `user:secret` read as
  host:port), and detecting exactly that is what now gates the fallback.

### Changed

- `ConnectionString` now documents the parsing contract it already enforced:
  the input must be a valid URI, and reserved characters in a password must
  be percent-encoded. There is no lenient mode and none is planned -- a
  password may itself contain `@` and `:`, so nothing can recover where an
  unescaped one ended, and a guess in the parser would be a guess about what
  to connect to. Documentation and tests only; behaviour is unchanged.

## [0.3.1] - 2026-09-22

### Fixed

- **A composite path falls back when SFTP cannot connect.** Nothing on the
  path route connected the provider first: pathlib_next dialled SFTP inside
  the operation, so an untrusted host key (`HostKeyNotVerifiable`) or a
  refused port escaped raw and the next path provider was never tried --
  while `run()` on the same host fell back. A composite path now calls the
  provider's `connect()` before dispatch, and SFTP maps asyncssh's connect
  errors the way the SSH leg already did. Most likely to fire since 0.3.0
  started verifying the SFTP host key: any host not yet in `known_hosts`.
- **`run()` no longer re-dials a declined provider on every call.** 0.3.0
  cleared every decline per call, so a dead primary cost a full connect
  attempt (~9 s against a refused port) on each `run()` while the fallback
  served. A decline now lasts for the generation again, and is re-admitted
  only when no other provider can serve -- which keeps a single-provider
  host from bricking on one transient refusal, for paths as well as `run()`.
- A path refusal with nothing left to try names why ("no path provider
  completed read (...: Host key is not trusted ...)") and keeps the cause.

## [0.3.0] - 2026-09-22

### Added

- **QGA paths have metadata and namespace operations.** `QemuHost.path()`
  now supports `stat`, `iterdir`, `mkdir`, `unlink`, `rename` and `chmod`
  through a guest-side helper built during discovery -- hostctl previously
  shipped none, so a caller had to write one and every QGA path reported
  `degraded`. The helper is POSITIVELY PROBED: GNU `stat -c`/`find -printf`
  on a POSIX guest, PowerShell on a Windows one. A guest that fails the
  probe keeps content access and loses only what genuinely needs a helper.
  `QemuConfig(path_helper=...)` still overrides.
- **`SshConfig(login_shell="auto"|"cmd"|"powershell"|"posix")`** says what
  parses the command string on arrival. Windows OpenSSH's stock server
  hands it to `cmd.exe /c`, so a `cmd`-dialect command line is parsed by cmd
  twice; unescaped, 4 of 8 adversarial values survived and two of the
  failures were command injection. `auto` reads the dialect.
- **`ShellFlavour.argument(value, *, target=ShellTarget...)`** separates
  quoting for the shell's parser from quoting for whatever reads the token
  next -- a cmd builtin, a child's argv parser, PowerShell's binder, or the
  program slot. `quote()` remains the syntactic layer.

### Removed

- **`SerialTransport`** and its `hostctl.executor` export. It duplicated
  `SerialProcess`'s locked, deadline-clamping byte path and had no caller
  outside its own tests, so the two could drift with nothing to notice.
  `SerialProcess` keeps the job -- it is what every console profile reads
  through -- and gained the `write(timeout=)` it lacked.
- **`SerialConfig.username` and `SerialConfig.password`.** They were stored,
  whitelisted for URI dispatch, and read by nothing: console credentials live
  in the profile's `login=` steps. An explicit one is now refused rather than
  silently ignored, and `HOSTCTL_PASSWORD` is no longer offered to a `serial:`
  URI.

### Changed

- The `pathlib_next` floor moves to `>=0.9.10` in both `dependencies` and
  the `ssh` extra. **0.9.4 through 0.9.9 cannot be used**: those releases
  decide "same file" and "overlapping trees" by falling back to path
  equality, which compares `(type, segments)` and knows nothing about
  hosts, so copying `/etc/app.conf` from one host to the same path on
  another was refused with `OSError [Errno 22] Source and target are the
  same file` (and `PathSyncer` with "source and target overlap"). 0.9.10
  adds the `_same_filesystem()` hook this release answers.
- **`LocalExecutor` merges `env` over the inherited environment** instead of
  replacing it, so the documented cross-transport rule -- "`env` is additive
  to the provider's environment" -- now holds locally too. A child no longer
  starts without `PATH`, `HOME` or `SystemRoot` because one variable was set.
- **A serial `run()` frames each command as its own profile exchange** and
  refuses a structured (argv) command: a serial host names no shell flavour,
  so it has no quoting rule to apply, and joining commands with `;` put two
  on one line where most device consoles read a single malformed one.
  `stdin=` and `bufsize=` raise instead of being ignored, as `input=` already
  did.
- **A serial `spawn(encoding=...)` reads text**, matching the SSH and QEMU
  console adapters; without either text keyword it still reads bytes.
- **QEMU `dialect="auto"`/`path_flavor="auto"` need positive evidence** of the
  guest's family -- `guest-get-osinfo`, or a family-exclusive command in
  `guest-info` -- and raise naming the explicit settings when neither is
  available. An agent with `guest-get-osinfo` blocklisted used to be read as
  POSIX, silently, including on Windows.
- **`QemuHost.info().os_family` is a family** (`linux`, `windows`,
  `freebsd`), taken from `kernel-name`. It carried `guest-get-osinfo`'s `id`,
  so the same machine reported `linux` over SSH and `ubuntu` over QGA.
- **Each guest-agent request is bounded by `agent_timeout`**, separately from
  a command's own `timeout=`: an agent that stops answering is a
  `ConnectionError` saying so, not a command timeout ten minutes later.
- **A truncated QGA `run()` result warns.** `stdout_truncated`/
  `stderr_truncated` were set and documented nowhere, so a shortened
  transcript looked complete.
- **`hostctl shell` opens a console that cannot allocate a terminal**, so a
  `serial:` URI works; **`hostctl cp local:/tmp/x`** is accepted, the
  spelling every other subcommand documents; and `hostctl --help` carries the
  URI forms, the `URI:PATH` grammar, the `HOSTCTL_PASSWORD` rule and the exit
  codes.
- **`asyncssh` and `libvirt-python` are bounded** (`>=2.14,<3`, `>=9.0,<12`);
  the `dev` extra now pulls every transport, so `pip install -e ".[dev]"` is
  the whole development environment.
- **`LocalHost` is a `SystemHost`.** It duplicated the provider selection,
  failover loop, capability computation and dispatch ladder, and the two
  copies diverged. Two consequences for callers:
  **`LocalHost().path()` with no segments no longer returns the absolute
  working directory** -- it returns a path with no segments added, like
  every other transport, which still resolves against the process's working
  directory. A local path is still a plain `pathlib_next` path, not a
  composite.

### Fixed

- **A transfer between two hosts is no longer mistaken for a file copied
  onto itself.** `CompositePosixPath`/`CompositeWindowsPath` now answer
  `pathlib_next`'s `_same_filesystem()` with the host's provider set, so
  two hosts are two namespaces even when a path is spelled identically on
  both, while two paths on one host still are one — which keeps the guard
  that refuses `copy()` of a file onto itself. A `.via()` pin selects a
  route to the host, not a different host, and answers accordingly.
- **The SFTP leg verifies the server's host key.** `SshConfig.connect_opts()`
  omitted `known_hosts` whenever it held its `()` default. asyncssh reads a
  missing key as "resolve known_hosts the usual way", so `run()` verified the
  server — but `pathlib_next`'s SFTP connect seeds `known_hosts=None`
  (verification off) for options it is not handed, so `path()` accepted any
  host key, including an actively substituted one, and sent it the configured
  password. Opting out is still possible and now explicit: `known_hosts=None`.
- **`ConnectionString` parses `user:password@host`.** A scheme-shaped username
  made `urlsplit` read `root:hunter2@nas` as the scheme `root`, so the password
  was never recognised: it stayed in the path, where `str()` and `repr()`
  rendered it verbatim, the host came out empty, and `is_local` reported a
  remote machine as local. Without a scheme to assume, a target carrying
  credentials is now refused rather than guessed at, and both error messages
  redact (`root:<redacted>@nas`).
- **An args-capable executor gets one shell layer, not two.** `SystemHost`
  fed a finished shell command line back into `flavour.invocation()`, which
  takes a script. On Windows the outer PowerShell re-parsed the inner
  `-Command` string, so `windows://node?executor=local` reported 0 for
  `cmd /c exit 3` — `check=True` passed for a failing command — and quoted
  text and variables came back mangled or empty.
- **cmd.exe builtin operands are quoted, and `set` no longer escapes inside
  quotes.** cmd splits a builtin's operands on `,`, `;` and `=` as well as
  whitespace, so `del /q a b.txt` deleted `a` and `b.txt` and left the named
  file. Separately, `set "KEY=VALUE"` caret-escaped inside a quoted span,
  where cmd does not process carets: `100%` reached the child as `100^%`,
  `%OS%` still expanded, and a `"` in the value ended the assignment early.
  An empty value remains inexpressible in cmd, and is now documented as such.
- **PowerShell 5 native arguments survive the C runtime.** PS 5.1 rebuilds the
  command line for a native program and leaves embedded quotes alone, so
  `run(("robocopy", src, dst, name))` with `name = 'my file" /MIR "z'` handed
  robocopy `/MIR` — mirror mode, which deletes files in the destination. Empty
  arguments were dropped and a trailing backslash swallowed the next argument.
  PowerShell 7 is unaffected and keeps the plain literal.
- **A transfer to another backend no longer renames inside the source host.**
  `ssh_host.path('/srv/export.csv').move(LocalPath('/home/op/export.csv'))`
  issued an SFTP rename on the server: the file left `/srv`, never arrived,
  and no error was raised. A `str` destination — documented by `pathlib_next`
  — also raised `TypeError` on every transport, and a cross-provider `move()`
  aborted instead of falling back to copy + remove. All three now work.
- **Docker file modes are translated.** The archive stat header carries Go's
  `os.FileMode`, used verbatim as a POSIX `st_mode`: `is_file()` was False for
  every ordinary file and `is_dir()` raised `OverflowError: mode out of range`
  for a directory, which also killed any recursive copy.
- **An unreachable QGA socket is reported as a transport failure.** It escaped
  as a bare `FileNotFoundError`, indistinguishable from the guest file being
  absent — so an append across a guest reboot staged an empty buffer and
  truncated the guest file when the agent came back.
- **Native WinRM reports the remote exit status.** The provider did not
  declare `manages_status`, so PowerShell's `; exit $LASTEXITCODE` epilogue was
  appended and its `exit` ended the remote pipeline before the
  `__HOSTCTL_LASTEXITCODE__` marker could be emitted. A command that failed by
  exit status reported 0 and `check=True` passed.
- **One unsupported operation no longer takes a path provider out of
  service.** A `NotImplementedError` from a retry-safe call was recorded as
  a decline on the host's shared provider selector, which every later
  operation then consulted: after a backend refused `samefile()` — it needs
  `st_dev`/`st_ino`, which many remote stats lack — the next read on any
  path of that host failed with `no path provider supports open_read`. The
  refusal is now scoped to the call that raised it. `OperationNotStarted`
  still declines the provider for the generation, as before.
- **A `cmd.exe` at a path containing a space is launchable.** The program
  slot of a Windows command line is read by `CreateProcess`, not by cmd, and
  rendering it with cmd's caret quoting made Windows look for a program named
  `^` -- measured as `FileNotFoundError: [WinError 2]`.
- **A structured PowerShell command can bind a named parameter.**
  `["Remove-Item", "-LiteralPath", path]` rendered every token as a quoted
  string, which PowerShell's binder reads positionally: measured against a
  real `powershell.exe`, it returned 1 and left the file in place. A token
  that is exactly a parameter name is now unquoted.
- **`ShellSession.send()` does not append a separator the text already has**,
  so `send("echo hi;")` is not `echo hi;;` and `send("sleep 5 &")` is not
  `sleep 5 &;`.
- **`zsh` quoting covers a leading `=`**, which zsh expands to a program path
  where POSIX `sh` leaves it alone.
- **`PowerShellFlavour(7, executable=...)` is named `pwsh`.** The name was
  derived only when no executable was given, so an explicit path left the
  flavour identifying as `powershell`.
- **`Shell(..., env={})` forwards no environment**, rather than an empty one
  that starts a child with nothing.
- **`normalize_input` covers `bytearray` and `memoryview`** -- the deadlock
  it exists to prevent, for the two buffer types it did not check.
- **Container archive reads no longer lie about links, sizes or errors**: a
  single-member hardlink archive raises `OSError(ENOENT)` instead of
  `tarfile.StreamError`, a hardlinked file in a listing reports its real
  size, the archive root (`"."`) is listable, and every Docker status code
  maps through one helper that populates `errno`.
- **A container exec stream no longer returns `''` mid-character**, and a
  closed container process reads EOF instead of raising the operating
  system's raw socket error.
- **QGA failures speak one error vocabulary**: a guest error is classified on
  the `strerror` tail (a file named `notfound.log` was reported missing
  whatever actually failed), `host.run()` of a missing guest program raises
  `FileNotFoundError`, libvirt-relayed guest errors stay guest errors, and a
  guest error no longer tears down and re-handshakes the connection.
- **One transport per QEMU host.** Lazy initialisation was unguarded:
  measured with six threads, six transports were built where one was wanted,
  each SSH one opening a connection nobody owned. A path handed out before
  `close()` now refuses instead of silently reconnecting.
- **A serial login failure raises `ConsoleProtocolError`**, not a bare
  `TimeoutError` that slipped past every documented handler, and a transcript
  attached to an error has `secret=True` login values redacted -- consoles
  echo what they are sent.
- **A QGA reader is seekable** where the agent advertises `guest-file-seek`,
  so `zipfile` and `tarfile` work against a guest path, and an exclusive
  `open("x")` is refused at `open()` rather than after the whole payload has
  been staged.
- **A `cmd`-dialect command sent over SSH to Windows is escaped for the
  login shell that receives it.** Two of the eight adversarial values were
  command injection, not corruption.
- **`SshConfig("ssh://host")` raises** instead of making the URI the
  hostname, which surfaced much later as an unresolvable name. The
  constructor takes a host; `HostConfig(uri)` takes a URI.

## [0.2.7] - 2026-08-16

### Changed

- Dependency floors now name versions hostctl is actually tested against.
  `pathlib_next` moves to `>=0.9.1,<0.10` and `netimps` to `>=0.2.0,<0.3`
  (the `ssh` extra's `pathlib_next[sftp-async]` moves from `>=0.8.4` to the
  same `>=0.9.1`). Both ceilings are unchanged, and no API hostctl calls has
  moved — this only stops resolvers from choosing an install that does not
  work.

  The `pathlib_next` floor is above the start of its minor series for one
  measured reason: `Path.symlink_to()` — and with it the `force=` keyword —
  first exists in **0.9.1**. The composite path forwards `symlink_to` to
  whichever backend path it was handed, so against a stock 0.9.0 `Path` the
  whole chain raises `AttributeError` rather than linking anything. The
  previous `>=0.8.6` floor therefore advertised support for installs where
  composite `symlink_to` could never work. `netimps` needs nothing past
  `0.2.0` and floors at the series start.

## [0.2.6] - 2026-08-16

### Added

- `wants_text(text, encoding, errors)` is exported from `hostctl.executor`,
  joining the four stream helpers made public in 0.2.5 for the same reason:
  an executor implemented outside hostctl must reach the same conclusions as
  the built-in ones, because a `SystemHost` chooses the provider.

### Fixed

- Direct execution no longer raises `NameError` on two dispatch paths.
  `ContainerHost.spawn(Exec(...))` raised on *every* direct spawn, and
  `SystemHost.run(Exec(program))` raised whenever the selected executor
  provider advertised neither `args` nor `script` — the shape a bare
  `ExecutorProvider("name", callable)` has, which is the documented minimal
  form of that public authoring contract. Both modules used `command_text`
  without importing it.

- `hostctl cp` no longer mistakes a URI port colon for the `URI:PATH`
  separator. `ssh://host:2222/tmp/x` split into `ssh://host` plus the relative
  path `2222/tmp/x` — the default port and the wrong file, with no message. A
  split that lands on the authority's port colon is now rejected with the
  grammar error, so the required spelling `ssh://host:2222:/tmp/x` is the only
  one that runs. Userinfo colons and IPv6 literals are excluded from the check.

- Native current-context WinRM now honours `server_cert_validation="ignore"`.
  It was spliced into the rendered wrapper with `str.replace`, which matched
  nothing without a port (the setting vanished) and produced an
  `Invoke-Command` parameter-binding error with one, since `SkipCACheck`
  belongs to `New-PSSessionOption`. The option is now built explicitly and
  passed as `-SessionOption`, skipping both the CA and CN checks to match
  pywinrm's `ignore`.

- Native current-context WinRM now reports remote exit codes.
  `Invoke-Command` does not copy the remote `$LASTEXITCODE` into the calling
  session, so a command that failed only by status — a native executable
  exiting non-zero without throwing — returned 0 locally and `check=True`
  never fired. The remote script block now emits the code as a
  `__HOSTCTL_LASTEXITCODE__` marker line, which the local wrapper consumes
  and exits with. **Behaviour change**: calls that silently succeeded against
  a failing remote command now raise `CalledProcessError` under the default
  `check=True`.

- Every provider now agrees on when output is text. `errors=` alone selected
  text mode on the local, WinRM, container, and QEMU executors and binary
  mode on SSH, PSRP, and the serial host, so the identical call returned
  `str` or `bytes` depending on which provider a `SystemHost` selected — the
  exact divergence the shared stream helpers exist to prevent. All seven
  sites now call `wants_text`. **Behaviour change**: `run(cmd,
  errors="replace")` returns `str` from SSH, PSRP, and serial where it
  previously returned `bytes`.

- SFTP paths percent-encode the remote path before embedding it in the
  `sftp://` URI. `pathlib_next` parses that URI and uridecodes its parts, so
  a filename containing `?` or `#` was truncated into a query or fragment and
  a literal `%xx` was decoded into a different name — reading and writing the
  wrong file with no error. Encoding is minimal (RFC 3986 `pchar`), so a
  Windows-flavoured remote path still reads as `sftp://host:22/C:/Temp`.

- `RunspaceSession` no longer closes a pool it was given. `_owns_pool` was
  recorded at construction and never read, so a pool injected to be shared
  across sessions was closed by whichever session finished first. An injected
  pool is now left open and retained, which also leaves the session
  reopenable; a pool the session created is still closed.

- An abandoned container or QGA write stream no longer uploads from the
  garbage collector. The staged write-back stream existed as three
  byte-identical copies of which only the WinRM one had a `__del__` guard;
  `io.IOBase.__del__` calls `close()`, and `close()` is what commits, so a
  write stream that went out of scope unclosed performed its network
  transfer at an arbitrary GC point with any error printed and swallowed by
  the interpreter. One `hostctl.host._staged_io` now serves all three, and
  the abandonment case warns instead of uploading. The `open()` mode
  validators were deduplicated with it, so `"rt"` is accepted on the
  container and QGA backends as it always was on WinRM.

- `SerialHost.run(capture_output=False)` no longer discards the console
  transcript. It reimplemented the output contract and treated a `None`
  stdout target as "discard"; the shared `dispatch_output` — and every other
  transport, and `subprocess` — treats it as `sys.stdout`. Serial now routes
  through the shared helper. `stdout=subprocess.DEVNULL` remains the way to
  discard deliberately.

## [0.2.5] - 2026-08-05

### Added

- `write_output`, `normalize_input`, and `dispatch_output` are exported from
  `hostctl.executor`, joining `capture_streams`. An executor implemented
  outside hostctl previously had to import `hostctl.executor._common` to
  reproduce hostctl's own stdout/stderr and stdin semantics.

  Sharing these is a correctness requirement rather than a convenience: a
  `SystemHost` can dispatch the same call through different providers on
  different attempts, so providers that disagree about output handling return
  results that differ by which transport won. `normalize_input` is the one
  worth not reimplementing — a mismatch there does not raise, it deadlocks,
  because bytes handed to a text-mode stdin kill `subprocess`'s writer thread
  without closing the pipe, so the child never sees EOF and `timeout=` never
  fires.

  No behaviour change; these are the same objects `_common` defines.

### Fixed

- Four conformance tests covering timestamp handling were skipping for a
  reason that was not true, so the contract they check went unverified. The
  checks called `os.utime()` on paths belonging to fake *remote* providers,
  which map into a private sandbox root and have no local existence; the
  resulting `FileNotFoundError` was reported as "this provider cannot set
  timestamps". Timestamps are now set through the sandbox that actually stores
  the file. Test-only change.

## [0.2.4] - 2026-08-05

### Fixed

- Directory listings no longer discard the backend's own `_scandir()`.
  `walk()` and `glob()` went through hostctl's `_scandir()`, which was a
  verbatim copy of `pathlib_next`'s generic fallback and called `iterdir()` —
  so a scheme whose listing already carries metadata never got to use it.
  `SftpPath._scandir()` reads every child's attributes in a single
  `listdir_attr` round trip; before this fix a remote `walk()` paid a listing
  plus one `stat()` per entry. `_scandir()` is now the routed primitive and
  `iterdir()` derives from it, matching the direction upstream intends.

- `copy()` and `move()` on a composite path now use the backend's own
  implementation when the destination resolves to a path that backend
  understands. They previously called `Path.copy(self, ...)` unconditionally,
  which bypassed every backend override — `SftpPath.copy()` fans out over
  asyncssh workers and `SftpPath.rm()`/`checksum()` run server-side, so the
  results stayed correct while the transport-native path was silently
  discarded. A genuine cross-backend transfer (a destination on another
  provider) still uses the generic implementation, which is what it is for.

### Changed

- Composite paths forward the method that was called to the selected backend
  instead of re-declaring a copy of `pathlib_next.Path`'s surface. Operations
  hostctl never declares — `touch()`, `rm()`, `lstat()`, `is_symlink()`,
  `chown()`, `checksum()` — now work through `host.path()`, and an operation
  added upstream is reachable without a new method here. `chown()`, added in
  `pathlib_next` 0.9.1, was unreachable before this.

  `_CompositePathMixin` drops from 47 to 36 methods. Operations with real
  composite behavior stay hand-written, each for a reason: `iterdir`/`_scandir`
  (rebuild children as composite paths), `rename` (cross-provider guard),
  `readlink` (rebuilds its result), `copy`/`move` (backend when the destination
  resolves on this provider, generic for a true cross-backend transfer), and
  `open` (the capability gate depends on the mode).

  The pure-path derivations (`parent`, `parents`, `joinpath`, `/`,
  `with_name`/`with_stem`/`with_suffix`, `relative_to`, `with_segments`) also
  stay: `pathlib.PurePath` builds those through `object.__new__`, bypassing the
  composite constructor, so they re-attach routing state that inheritance drops
  rather than duplicating anything.

- Provider fallback now also triggers on `NotImplementedError`, but only for
  operations that cannot mutate before raising (reads, `stat`, `chown`,
  `chmod`). Writes and composed wrappers still propagate it: a wrapper built
  from several primitives may have already changed something when a later
  primitive raises — `symlink_to(force=True)` unlinks before calling
  `_symlink_to()`, so a backend lacking that primitive deletes the entry and
  only then fails. Retrying that against another provider would repeat the
  work with the original already gone.

## [0.2.3] - 2026-08-04

### Fixed

- Composite paths no longer drop backend-specific keyword arguments.
  `CompositePosixPath.symlink_to()` accepted only the stdlib signature and
  forwarded nothing else, so a backend's documented extension was unreachable
  through the very abstraction meant to expose it — a path obtained from
  `host.path()` raised `TypeError: unexpected keyword argument 'force'` even
  when the selected backend implemented `force=`.

  Forwarding is **signature-aware** rather than blind: the selected backend's
  method is inspected, and only keywords it declares are passed through.
  Anything else still raises `TypeError` at the composite boundary, naming
  the backend class and the rejected keyword. Blind passthrough would have
  turned a clear error at the abstraction boundary into a confusing one from
  inside a transport, and the existing contract — a backend lacking a
  capability raises `NotImplementedError`, never a silent no-op — is
  unchanged. A method whose signature cannot be introspected (a C function,
  a `functools.partial`) receives the keywords, since an error from it is no
  worse than calling it directly.

  Applied to `mkdir()`, `chmod()`, `unlink()`, and `rmdir()` alongside
  `symlink_to()`, since the same normalization affected each of them.

  This pairs with `pathlib_next`'s `symlink_to(force=)`, which that project
  exposes as a generic `Path` extension over a `_symlink_to()` backend
  primitive. No version floor change: hostctl's `pathlib_next>=0.8.6` floor
  stays where it is, so `force=` is forwarded when the installed version
  provides it and rejected at the boundary when it does not.

## [0.2.2] - 2026-07-29

### Changed

- Dependency ranges widened to admit `pathlib_next` 0.9 and `netimps` 0.2:
  `pathlib_next>=0.8.6,<0.10` and `netimps>=0.1,<0.3`, with the `ssh` extra's
  `pathlib_next[sftp-async]` bound moved to `<0.10` alongside it. The
  previous `netimps<0.2` also conflicted with `pathlib_next` 0.9's own `uri`
  extra, which requires `netimps>=0.2.0`.

  The floors stay where they were rather than rising to the new minors:
  hostctl uses no API added in either release — its three netimps functions
  (`get_default_port`, `is_local_address`, `try_parse`) all exist in 0.1, and
  `ProgressReader` is hostctl's own wrapper, unrelated to pathlib_next 0.9's
  native `copy(progress=)`. A floor demanding versions the code does not need
  would exclude working installs for nothing. The ceilings span two minors
  because both projects are pre-1.0, where a minor may break.

  Verified against both ends of the range: the suite passes with
  `pathlib_next` 0.8.6 and 0.9.0, each alongside `netimps` 0.2.0.

## [0.2.1] - 2026-07-29

### Added

- `ConnectionString` parses a connection target from whatever a user typed.
  `ConnectionString("nas", scheme="wss")` and `ConnectionString("nas:8443",
  ...)` parse, because a bare host is not an invalid URI — it is a URI with
  the scheme left off, which is what people type on a command line.

  Every field can be supplied directly, and three layers decide each one: an
  explicit argument wins, then whatever the string carried, then `defaults`.
  `scheme=`/`port=`/`host=`/… are therefore *overrides* — `scheme="ssh"`
  beats a `wss://` in the string — while `defaults` (a mapping, or another
  `ConnectionString` used as a profile) fills only what nothing else
  supplied.

  A port carries its own resolution strategy wherever one is accepted: an
  `int`, a callable `scheme -> port | None`, or anything indexable by scheme
  (a plain mapping), so a caller passes its whole table without pre-selecting
  an entry. When no layer supplies one, `netimps.get_default_port` resolves
  the scheme — it knows schemes no system services database does (`ws`/`wss`,
  `socks5`), and an application can register more with
  `netimps.register_port`.

  `False` stops the search outright: no port, and no lookup.

  Credentials are parsed but never rendered. `str()` and `repr()` both emit
  the redacted form, with the password **removed rather than masked**, so the
  output stays a valid, reusable connection string that cannot round-trip a
  wrong credential — and a value reaching a log line or a traceback frame
  cannot leak one. A password may carry `key:value` extras after a newline,
  as `parse_credentials` describes, written raw.

  The host keeps the spelling it was given. `is_local` resolves nothing — it
  is called while a configuration is built, which is network-free, and
  resolving would both block and raise on a name that does not resolve. An
  address literal is answered by `netimps.is_local_address`, so an address
  actually assigned to this machine counts as local and not just loopback,
  while a name is compared against the loopback spellings. `qsl` and
  `query_val()` read the query; `replace()` returns a changed copy.

### Changed

- **`netimps` is now a required dependency**, supplying scheme/port and
  address semantics rather than hostctl reimplementing them. Note this
  arrives in a patch release: an existing install pinned within `0.2.x` gains
  a new requirement, so a locked or offline environment needs `netimps`
  available before upgrading.

### Fixed

- A path in a command is rendered through `__fspath__` rather than `str()`.
  Every transport now shares one `command_text` helper — the SSH, WinRM,
  container, QGA, and PSRP executors each stringified with `str()`, so only
  the local executor asked for the filesystem representation. `__fspath__` is
  tried by attribute rather than by `isinstance(value, os.PathLike)`, so a
  duck-typed path that never registered with the ABC is honoured too, and an
  object whose `__fspath__` raises or returns a non-string falls back to
  `str()` rather than failing the command. The shell layer follows the same
  rule. No shipped path type changes behaviour — `str()` and `__fspath__`
  agree for all of them — but the contract now matches what a path promises.

## [0.2.0] - 2026-07-28

### Added

- `Exec(program, *args)` marks one command for direct execution: the program
  runs with that argv and no shell layer renders. The program and every
  argument may be a `str`, `bytes`, or a path object — all reach the transport
  as text, so the spelling records how the caller held the value rather than
  changing what runs. A **bare program name is now possible**
  (`Exec("ls", "-l")`), resolved through the target's `PATH`; it previously had
  no spelling at all, because a plain string command is always shell text.
- The full `run()` command syntax is documented in the "Running commands"
  guide: the varargs rule, the three command forms, operators, validation, and
  the PowerShell rendering.

### Changed

- **Breaking.** Direct execution is marked by `Exec`, not by position. A path
  in the first argument used to mean "this is the executable, the rest is
  argv"; a path is now an ordinary value that stringifies wherever it appears.

  ```python
  host.run(Path("/bin/ls"), "-l")   # before: direct execution
  host.run(Exec("/bin/ls", "-l"))   # now
  ```

  This is a **silent** change for the old spelling: `run(Path(...), *args)`
  still succeeds, but the values become several `;`-joined shell commands
  instead of one program and its argv, so arguments are subject to shell
  quoting and word-splitting. Anything passing a leading path must be updated;
  there is no deprecation shim.

  What it buys, both unspellable before: several path commands in one call
  (`run(p1, p2)` is two commands), and direct execution of a `PATH`-resolved
  name.

  An `Exec` cannot be combined with other commands in one call — there is no
  shell to join them with, and running only one would silently drop the rest.
  It raises `TypeError` rather than choosing.
- `hostctl run` passes its operand as a single `Exec`, so the program is used
  verbatim. It no longer converts the operand through the target shell's path
  flavour, which means a bare name now resolves through the target's `PATH`
  instead of being treated as a relative path.

## [0.1.2] - 2026-07-28

Supersedes 0.1.1, which was tagged but never published: a release-gate test
timed out on the Windows/Python 3.14 runner, so nothing reached PyPI. There is
no 0.1.1 release; its fix is included here.

### Added

- `uri_hostname(parsed)` returns the host as it was written in a URI, rather
  than the case-folded spelling `urlsplit().hostname` produces. Use it in
  `_from_parsed_uri` wherever a host is stored; presence checks can keep using
  `.hostname`, since emptiness does not depend on case.

### Changed

- A config built from a URI now stores the hostname as typed, so
  `HostConfig("ssh://nasA")` keeps `nasA` in `.host`, in `connection_uri`, and
  in the `HostInfo.hostname` a system host reports without connecting. It
  previously stored `urlsplit`'s lowercased form, which meant the library
  echoed a spelling the operator never wrote and left downstream code to
  recover the original from the URI itself.

  Consequently `.host` is the spelling that was given, not a canonical form:
  `HostConfig("ssh://nasA")` and `HostConfig("ssh://nasa")` no longer compare
  equal, so code using a config or its host as a dict key or for
  deduplication should casefold explicitly. Resolution is unaffected — DNS,
  SSH, and WinRM all treat the two spellings as one name.

### Fixed

- `redact_uri()` no longer case-folds the hostname. Only the branch that
  rebuilt the authority — the one taken when a password was present — adopted
  `urlsplit`'s lowercased `hostname`, so `nasA` rendered as `nasa` with a
  credential and as `nasA` without one. The same host now renders one way
  whichever branch runs, and log records stay greppable by the name the
  operator typed. Redaction removes a credential; normalizing a host is a
  separate concern and is left to the transport that resolves it.
- The spawn conformance test no longer fails on a slow runner. It waited 10s
  for a real `powershell.exe -NoProfile` to start, exit, and be reaped, which
  a cold Windows CI runner can exceed. The wait is a hang guard rather than a
  performance assertion, so it is now 60s. Test-only; no library change.

## [0.1.0] - 2026-07-27

First release. Alpha: the public surface is deliberately small (66 exported
names) and may still change, but everything documented here is covered by the
test suite on Python 3.9 through 3.14.

### Added

- Protocol-agnostic `Host` contracts with secret-safe `HostConfig` URI
  dispatch, lifecycle management, normalized host information, and explicit
  capability reporting.
- Local, SSH, WinRM, Docker Engine container, and QEMU Guest Agent hosts with
  buffered command execution and `pathlib_next.Path` filesystem backends where
  the transport supports them. WinRM includes a Windows-semantic PowerShell
  path backend; container and QEMU paths use archive and guest-agent file RPCs.
- Explicit and auto-detected shell dialects (POSIX, Bash, Zsh, Fish, CMD, and
  PowerShell), shared structured quoting, environment/cwd helpers, operators,
  and persistent SSH/container sessions with optional terminals.
- Raw serial and QEMU serial-console transports with validated UART settings,
  exclusive process leases, stream lifecycle controls, and explicit
  non-shell semantics until a console profile is supplied.
- A dependency-free `hostctl` command with run, path inspection/copy,
  host-info, and interactive shell subcommands; passwords are accepted only
  from the environment or a hidden prompt.
- Optional native integrations: AsyncSSH/SFTP, pywinrm, Docker SDK, PySerial,
  pypsrp, and libvirt QGA, each isolated behind a matching package extra.
- Cross-host `pathlib_next.Path.copy()`/`PathSyncer` support with streaming
  remote readers, executor-side checksums, fast stat checksums, and an explicit
  progress-reader recipe.
- Transport-independent POSIX, Windows, and IOS host semantics with ordered
  executor/path provider selection and capability-safe fallback behavior.
  Providers declare per-operation capabilities, so a read-only backend rejects
  a mutation explicitly instead of falling through to another provider.
  Selection traces record the candidates, probe result, chosen provider,
  generation, policy, and pin, with credential-like values redacted. Composite
  paths keep their provider collection and optional `.via()` pin through `/`,
  `joinpath`, `parent`/`parents`, `with_name`/`with_suffix`, `iterdir`,
  `glob`/`rglob`/`walk`, and open streams. See the "Systems and providers"
  guide for the no-replay safety rule and provider-authoring contract.
- Symbolic-link support on every path backend whose transport provides it.
  `symlink_to(target, target_is_directory=False)` and `readlink()` follow the
  `pathlib.Path` signatures, `readlink()` reports the stored target verbatim,
  and `stat(follow_symlinks=...)` stays consistent with `is_symlink()`. Local
  paths delegate to `os.symlink`, SFTP uses the SSH backend's
  `symlink`/`readlink`, WinRM issues `New-Item -ItemType SymbolicLink`
  (normalizing the Windows elevation/Developer-Mode requirement to
  `PermissionError`), and container paths ship a `SYMTYPE` tar member through
  `put_archive()`. QGA paths raise `NotImplementedError` because the guest
  agent exposes no symlink RPC. Container reads now follow a symlink member to
  its target instead of failing, without giving up streaming laziness.
- Subprocess-shaped execution options, normalized transport errors, bounded
  buffered file transfers, and Python 3.9+ typing support (Python 3.14 is the
  default development interpreter).
- `Shell` is a context manager: `with host.shell as session:` opens one
  default session and closes it on exit, the no-argument shorthand for
  `shell.session()`. Re-entering a shell whose session is still open raises
  rather than sharing or leaking a process.
- `Shell` carries defaults. `host.shell(cwd=..., env=..., encoding=...,
  errors=...)` returns a shell applying them to every later `run`/`session`
  that omits its own value; `configure(...)` derives a further-configured copy
  without mutating the original. `cwd`/`encoding`/`errors` are replaced by a
  per-call value, `env` merges per key so one variable can change without
  restating the rest, and `env=None` declines the shell's environment while
  keeping whatever the host itself provides.
- Connection URIs may carry credentials. `scheme://user:password@host` is
  accepted: the password is extracted into the credential arguments and
  stripped from the parsed authority, so it never reaches a field that
  `connection_uri` or `repr()` renders. `redact_uri()` removes a password and
  returns a valid, reusable URI rather than masking it, so a rendered form can
  never round-trip a wrong credential.
- `parse_credentials()` splits a password field on a newline into the password
  and trailing `key:value` extras, so an OTP or other second factor travels
  through a single field. A bare name is a flag equivalent to `name:`. Inside
  a URI the separator may be written raw — tab, CR, and LF are percent-encoded
  before parsing, since `urlsplit` deletes them silently. A control character
  in the *host* is rejected, because deletion there rewrites the target.
- `ssh_providers()` and `winrm_providers()` return an executor/path provider
  pair sharing one transport, for composing a transport into a host you
  assemble yourself. `strict_uri_credentials`, `strict_uri_query`, and
  `uri_host` are public for configs implementing `_from_parsed_uri`.
- A `HostConfig` subclass may declare `uri_credentials`; dispatch then rejects
  any other credential before construction, so a typo fails loudly instead of
  silently producing a config with no password.
- SSH execution now sends EOF for omitted stdin, rejects unsupported zero
  buffering, normalizes missing exit statuses to `-1`, performs best-effort
  timeout cleanup (with `TimeoutExpired.orphaned`), and normalizes persistent
  process I/O failures. SFTP backends are reused per host and invalidated on
  close; auto-dialect probes preserve the discovered shell executable.
- Container archive paths now accept normal absolute and relative symlink
  targets, resolve links with a bounded hop count, preserve hardlink/file
  semantics, use Docker's header stat metadata where available, and reject
  traversal names without buffering directory archives unnecessarily. Docker
  exec streams return available data promptly, detect truncated frames,
  preserve merged output ordering, and map missing containers to
  `ConnectionError`.
- QEMU Guest Agent transports now share one framed-session implementation with
  split-read buffering, parse-error correlation, safe timeout/disconnect
  cleanup, and loop-bound SSH writes. QEMU serial consoles and raw serial
  processes use incremental text decoding and the common `read(-1)` contract
  (up to 64 KiB available data); serial ownership and QEMU URI/lifecycle edge
  cases are normalized consistently.

### Fixed

- `run(input=<bytes>, encoding=...)` no longer hangs. Handing bytes to a
  text-mode `subprocess` stdin killed its writer thread, and the call then
  blocked forever because the child never saw EOF — `timeout=` did not fire.
  `input` is now normalized to the stream mode each executor uses, by a helper
  the local, SSH, and QGA executors share, so the same call behaves
  identically whichever provider a `SystemHost` selects.
- Composite host paths kept their provider, selector, and pin through
  `pathlib.PurePath` derivations (`parents`, `with_name`, `with_suffix`,
  `relative_to`). Those results were previously built without any routing
  state, which made `glob()`, `rglob()`, and `walk()` fail outright.
- A path provider that declined before dispatch is remembered for the
  connection generation instead of being re-attempted by every later
  operation; `invalidate()` clears the record along with cached probes.
- `SystemHost` serializes its connection bookkeeping under a reentrant lock.
  Concurrent `run()` calls previously raced the check-then-append in
  `_ensure_provider_connected`, so every caller repeated the connect
  round-trip and appended a duplicate entry to `_connected_providers`, which
  grew without bound. Provider membership is now tested by identity.
- Capability vocabularies agree. `ExecutorCapability` members subclass `str`,
  so the enum published by executors and the strings published by providers
  and hosts compare equal. `Shell` previously tested `ExecutorCapability.CWD`
  against a set of strings — always false — so a host with native `cwd`/`env`
  still had `cd`/`export` rendered into the script and the native values
  dropped.
- `SystemConfig` no longer fails with `AttributeError`. It is explicitly
  abstract: `_create_host()` raises `TypeError` naming the concrete
  configurations, and it no longer advertises a `system://` URI that
  `HostConfig` rejected as an unsupported scheme.
- `scheme` is a URI-derived property across the whole `HostConfig` hierarchy.
  The system configurations shadowed it with a plain string and `SystemHost`
  assigned to it; a config-less host now builds its own family configuration
  instead.

[Unreleased]: https://github.com/jose-pr/hostctl/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/jose-pr/hostctl/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/jose-pr/hostctl/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/jose-pr/hostctl/compare/v0.2.7...v0.3.0
[0.2.7]: https://github.com/jose-pr/hostctl/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/jose-pr/hostctl/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/jose-pr/hostctl/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/jose-pr/hostctl/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/jose-pr/hostctl/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/jose-pr/hostctl/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/jose-pr/hostctl/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jose-pr/hostctl/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/jose-pr/hostctl/compare/v0.1.0...v0.1.2
[0.1.0]: https://github.com/jose-pr/hostctl/releases/tag/v0.1.0
