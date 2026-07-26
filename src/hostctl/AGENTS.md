# hostctl — API header

Protocol-independent host execution and filesystem paths. Core requires
`pathlib_next`; transport integrations are optional extras.

The dependency-free CLI entry point is `hostctl._cli:main`. Commands are
`run`, `ls`, `cat`, `cp`, `info`, and `shell`; passwords come only from
`HOSTCTL_PASSWORD` or `--ask-password`, never argv values.

`hostctl.__all__` is the stable surface: hosts and configs you construct,
exceptions you catch, types you annotate with, and the provider/shell contracts
you implement. Concrete backends, transport adapters, and objects the library
only hands back (`QgaPathBackend`, `WinRMPathBackend`, `ContainerPathBackend`,
the `Posix*`/`Windows*` path classes, the concrete `*Executor`s, `*Process`
classes, and `hostctl.provider.transports`) stay importable from their defining
module but are **not** exported from `hostctl` and may change without notice.

Host implementations are grouped under `hostctl.host`: shared contracts are
re-exported from the package, with concrete implementations in private
modules (`hostctl.host._local`, `._ssh`, and `._winrm`) plus the other provider
modules. WinRM paths live with `_winrm`; QGA paths live with `qemu`.
The private `hostctl.executor._qga` module owns QGA framing and its Unix,
libvirt, and SSH transports; it is consumed by the QEMU executor and host.
Shell construction is transport-independent under `hostctl.shell`:
`_common.py` owns shared contracts, while `posix.py` and `powershell.py` own
their concrete flavours. Executor code follows the same layout under
`hostctl.executor`: `_common.py` owns contracts and option types; `ssh.py` and
`winrm.py` own `SshExecutor` and `WinRMExecutor`. Each package re-exports its
own contracts; only the stable subset above reaches top-level `hostctl`.
`hostctl.sync` adds `stat_checksum(entry)`, `host_checksum(*hosts,
algorithm="md5", chunk_size=1048576)`, and `ProgressReader`; these plug into
`pathlib_next.utils.sync.PathSyncer` and the existing path copy machinery.

`SystemHost`, `PosixHost`, and `WindowsHost` compose ordered executor/path
providers. `PosixHost.from_ssh(SshConfig(...))` and
`WindowsHost.from_winrm(WinRMConfig(...))` retain the original connection URI
and lifecycle while exposing transport operations through provider adapters;
transport implementations remain private.
`register_system_provider(name, resolver)` extends logical system URI
descriptors. Built-ins include `local`, `ssh`, `sftp`, and `winrm`; transport
descriptors require matching objects in `SystemConfig(provider_options=...)` and
never serialize credentials into the canonical URI.

`Executor(command, *, stdin=None, stdout=None, stderr=None, cwd=None, env=None,
capture_output=None, check=None, encoding=None, errors=None, input=None,
timeout=None, text=None, **options)` defines the shared shell-agnostic option
surface. `ExecutionOptions` is the corresponding total-false `TypedDict`;
executor-specific extensions remain keyword options.
`ExecutorCommand` is `str | pathlib.PurePath | pathlib_next.Pathname`; paths
remain path objects until the concrete executor converts them for transport.
`ExecutorCapability.ARGS`, `.CWD`, and `.ENV` declare native executor support.
`Shell.execute(path, *args)` preserves path/args for an `ARGS` executor;
otherwise the flavour safely renders them into one script. `cwd` and `env`
follow the same native-or-embed rule.
`Shell`, `SshExecutor`, and `WinRMExecutor` inherit the `Executor` protocol so
shared protocol implementations can be added once; `Shell.__call__` delegates
to `Shell.execute()`.
`Host` itself supplies the multi-command `run()` provider contract; there is no
duplicate host-executor protocol or unused host-options wrapper.
`Shell.execute(path_like)` preserves the path, while string input remains a
string. Executors and hosts distinguish direct commands from shell scripts by
that value type; no duplicate command-kind flag is carried.

## Configuration and lifecycle

- `Host(connection_string, **secrets) -> Host` dispatches a secret-safe URI
  through config implementations and the `hostctl.configs` entry-point group.
- `HostConfig(connection_string, **secrets) -> HostConfig` performs the same
  dispatch without creating a host. `str(config)` is its canonical,
  secret-free connection string and can be passed back to `HostConfig`.
- `config.connection_uri` never includes passwords or private keys;
  `config.scheme` matches its URI scheme.
- `with config as host:` and `with config.open() as host:` connect and always
  close. Re-entering the same active config raises `RuntimeError`.
- External config implementations declare `schemes=(...)`; registry hooks and
  caches are protected implementation details.

`LocalConfig`, `SshConfig`, `WinRMConfig`, and `ContainerConfig` produce their
corresponding hosts.

## Shell contract

- `Shell(flavour, executor)` binds a `ShellFlavour` to either a command
  callable or an object exposing `run(command, **options)`. The command is
  always one string. If its inspected signature accepts `cwd` and/or `env`,
  `Shell.run()` forwards those separately; otherwise the flavour embeds that
  context into the script. Shell-agnostic subprocess options (`stdin`,
  `stdout`, `stderr`, `capture_output`, `check`, `encoding`, `errors`, `input`,
  `timeout`, `text`) pass through only to the executor. `Shell.execute(command)`
  passes strings unchanged and converts path-like commands with `str(path)`.
- `ShellFlavour.script(cmds, *, cwd=None, env=None) -> str` constructs a script
  in one target shell language.
- `ShellFlavour.environment_script(env) -> str` is a reusable standalone
  environment-mapping renderer. The base normalizes/validates variable names
  and joins each flavour's `environment_assignment(key, value)` output.
  Values remain objects so a flavour can preserve meaningful types; built-ins
  decode bytes and stringify ordinary values.
- `ShellOperator.PIPE`, `.AND`, `.OR`, `.REDIRECT`, `.APPEND`, and `.SEQUENCE`
  are explicit infix tokens between top-level commands. Flavours own their
  spelling and may reject operators they cannot represent portably.
- Raw strings stay verbatim; tuple/list commands quote each item as data;
  standalone and structured paths are quoted; top-level commands otherwise
  use the flavour's `command_separator`.
- `ShellFlavour.command(cmds, *, executable=None, cwd=None, env=None) ->
  ShellCommand` wraps that script for an SSH exec channel.
- Structured values are normalized through the base class (including bytes and
  iterable argv sequences); empty structured commands and control characters
  are rejected. Raw empty command strings are skipped when joining.
- Environment assignments embedded by a shell are additive to the inherited
  remote environment. This intentionally differs from local
  `subprocess.run(env=...)`, which replaces the environment; a clear-env mode
  remains a separate future contract.
- `POSIX_SHELL` and `POWERSHELL` are the built-in strategies;
  common built-ins also include `BASH`, `ZSH`, `FISH`, `CMD`, and PowerShell 7
  `PWSH`. PowerShell 5 rejects `AND`/`OR`; PowerShell 7 supports them.
- `shell_flavour(selection)` accepts a registered string, configured
  `ShellFlavour` instance, or no-argument `ShellFlavour` subclass.
  `register_shell_flavour()` adds application-defined string selections.
- `ShellCommand.command` is transport-ready text; `.environment` is the
  environment sent out of band, or `None` when embedded into the script.
- `Shell(flavour, executor, cwd=None, env=None, encoding=None, errors=None)`
  accepts defaults applied to every `run`/`session` call that omits them.
  `host.shell(cwd=..., env=...)` returns such a shell; bare `host.shell` has
  none. `cwd`/`encoding`/`errors` are replaced by a per-call value, `env`
  merges per key, and `Shell.configure(...)` returns a configured copy without
  mutating the original.
- `Shell.session(*cmds, terminal=False, cwd=None, env=None, ...) -> ShellSession`
  opens a persistent provider process. `Shell` is also a context manager:
  `with host.shell as session:` opens a default session and closes it on exit,
  which is the no-argument shorthand for `with host.shell.session() as ...`.
  Re-entering a shell whose session is still open raises `RuntimeError`.
  `ShellSession.send(*cmds, cwd=None,
  env=None)` uses the same command grammar, writes the flavour terminator and
  a line terminator, and mutates the live shell context. This newline is
  required for interactive shells to submit each command.
  TTY stderr is merged into stdout.

## Host contract

- `Host` is abstract. Base `run()` and `path()` raise `NotImplementedError`.
- Hosts expose delegated `scheme`/`connection_uri`, explicit `capabilities`,
  `info() -> HostInfo`, and `connect()`/`close()` plus context management.
- `host.shell_flavour` is the explicitly known target-shell strategy;
  `host.shell` builds `Shell(host.shell_flavour, host)`. SSH uses its configured
  or positively detected flavour, WinRM uses PowerShell, and local execution
  selects POSIX sh or Windows PowerShell from the local platform.
- SSH provider run renders through its shell flavour and delegates the finalized
  command to `SshExecutor`; WinRM provider run delegates its finalized
  PowerShell script to `WinRMExecutor`.
- `HostInfo` fields are optional; unknown system values remain `None`.
- A usable `path()` returns `HostPath` (`pathlib_next.Path`).
- A usable `run()` returns `subprocess.CompletedProcess`; `check=True` raises
  `subprocess.CalledProcessError`, and command timeouts raise
  `subprocess.TimeoutExpired`.
- `Host.spawn()` is the low-level persistent `Process` contract. Providers
  advertise `spawn` and `tty` separately.

## SSH

`SshConfig(host, port=22, username="root", password=None, client_keys=None,
executable=None, known_hosts=(), dialect=POSIX_SHELL,
path_flavor=pathlib_next.PosixPathname)`.

Authentication fields are explicit and excluded from repr. `dialect` selects
POSIX or PowerShell command construction independently of the POSIX/Windows
SFTP path flavor. `dialect` is a `ShellFlavour` strategy. `path_flavor` is a concrete
`pathlib_next.Pathname` or `pathlib.PurePath` subclass; bare `PurePath` is
rejected because it would infer the local OS. SSH implies neither an OS nor a
shell. `dialect="auto"` performs positive cached probing and raises rather than
guessing. The SSH executor provider closes its AsyncSSH connection and waits for closure.
AsyncSSH authentication failures are exposed as `PermissionError`; SSH
host-key, key-exchange, disconnect, connection-loss, protocol, and channel
failures are exposed as `ConnectionError`. The original AsyncSSH exception is
retained as `__cause__`.
The SFTP path provider reuses one `AsyncsshSftpBackend` per host and invalidates its
cached sources during `close()`; each path call does not create another SFTP
connection. Provider close performs all AsyncSSH operations through the
shared bridge. Omitted `run()` stdin is an explicit EOF stream, `bufsize=0` is
rejected, and a missing remote exit status is reported as return code `-1`.
Timeouts raise `subprocess.TimeoutExpired` with an `orphaned` flag indicating
whether a process/channel termination hook was available. `dialect="auto"`
retains the executable path reported by the successful probe. Persistent SSH
process reads, writes, EOF, and close operations use the same transport-error
normalization as `wait()`.

## Containers

`ContainerConfig(container, engine_url=None, user=None, workdir=None,
executable=None, dialect="auto", path_flavor="auto")` uses the optional
`container` extra and Docker Engine API. Inspection selects Linux/POSIX or
Windows/PowerShell semantics. `ContainerHost` supports buffered exec,
persistent sessions/TTYs, and archive-backed POSIX or Windows paths. Archive
paths support stat/traversal/read/write/append/exclusive-create; archive-only
mkdir/remove/rename/chmod raise `NotImplementedError`.

## QEMU Guest Agent

`QemuConfig(domain, transport="libvirt", connection=None, socket_path=None,
ssh=None, agent_timeout=10, dialect="auto", path_flavor="auto")` creates a
`QemuHost`. Transports are local libvirt (`qemu-libvirt` extra), direct Unix
socket, or an AsyncSSH-tunneled remote Unix socket. Discovery positively probes
QGA and its enabled command list.

`QemuExecutor` uses buffered `guest-exec`/`guest-exec-status`. QGA cannot cancel
timed-out processes; `TimeoutExpired.orphaned` is true and `.pid` is retained
when known. `QgaPathBackend` uses bounded file-handle RPCs; metadata/mutations
without a positively available helper raise `NotImplementedError`.
An injected `QemuSerialConsole` adds the `serial` capability and
`QemuHost.open_serial()`. It is raw, exclusive, and makes no shell/status claim.

## WinRM

`WinRMConfig(host, username, password=None, transport="ntlm", port=None,
ssl=False, server_cert_validation="validate", message_encryption="auto",
operation_timeout_sec=20, read_timeout_sec=30, provider="auto")`. `auto`
selects PSRP when `hostctl[psrp]` is installed on Python 3.10+, otherwise
pywinrm; `provider="psrp"` requires the extra.

The WinRM executor provider supports PowerShell `run()` and Windows-semantic `WinRMPath`.
Password-free configs on Windows use current-context native PowerShell
remoting; explicit credentials use pywinrm. `WinRMPath.open("rb")` fetches
bounded ranges; writable modes stage content and transfer Base64 chunks on
close. WinRM stdin and command
deadlines remain unsupported. Transport timeouts are not a total command
deadline. pywinrm Session has no guaranteed close API; hostctl calls `close()`
only when a provided session exposes it.
PSRP runspaces are exposed separately through the WinRM transport provider and
`RunspaceSession.invoke()`. They retain typed PowerShell streams and state, and
are not advertised as a byte-oriented `spawn`/TTY process.

## Local and utilities

`LocalHost.path()` and `LocalHost.run()` work on POSIX and Windows.
`LocalExecutor` provides native argv, cwd, environment, stream, encoding,
check, and timeout behavior through `subprocess.run`.

`SerialConfig`/`SerialHost` provide an opaque `serial:///...` URI and one
exclusive byte-stream lease. `RawConsoleProfile` supports sessions only;
`PromptConsoleProfile` adds bounded login/prompt framing and advertises
`run` only when `reliable_status=True` and a completion marker is configured.
Streams are merged, PTY/path/status semantics are absent unless the profile
explicitly supplies them. Optional PySerial support is the `serial` extra and
injected serial objects remain caller-owned. Break, DTR, and RTS are available
on `SerialProcess`/`SerialConsoleProcess`; RFC 2217 URLs are not encrypted.
