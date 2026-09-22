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
the `Posix*`/`Windows*` path classes, the concrete `*Executor`s **other than
`LocalExecutor`**, `*Process` classes, and `hostctl.provider.transports`) stay
importable from their defining module but are **not** exported from `hostctl`
and may change without notice. `LocalExecutor` is the exception and is
deliberately exported: it is the one executor a caller constructs directly, to
compose a host out of providers without a transport.

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
A shell flavour answers TWO quoting questions, not one. `quote(value)` is
syntactic -- enough that this shell sees one word and expands nothing.
`argument(value, *, target=ShellTarget...)` quotes for whatever parses the token
NEXT: `SHELL` (a cmd builtin, split by cmd itself), `NATIVE` (a child's own argv
parser -- the C runtime on Windows, nothing on POSIX), `CMDLET` (PowerShell's
binder) or `PROGRAM` (the program slot, read by `CreateProcess`). Where the two
layers coincide -- every POSIX-family shell, since `execve` takes a vector --
the default `argument()` is just `quote()`. `command_target(values)` infers the
target where it is decidable (cmd knows its own builtin list) and answers
`NATIVE` otherwise; PowerShell deliberately does NOT guess whether a name is a
cmdlet, because that needs a runspace.

`cwd_guard` declares how a failed `cd` is stopped from running the payload:
`"operator"` fuses the change onto a GROUPED payload with the AND operator,
`"statement"` is for a flavour whose change-directory statement aborts the
script itself (PowerShell's `-ErrorAction Stop`). It is declared rather than
inferred from `context_order`'s ordering, which is how PowerShell's `join_cwd`
override stayed unreachable for years.

`hostctl.executor`'s public helpers are the ones a transport adapter needs to
behave like the others; an executor that skips them is where cross-transport
divergence comes from:

- `normalize_input(input, *, text_mode, encoding=None, errors=None)` -- match
  `input` to the stream mode about to be used. A mismatch is not a clean
  error: the writer thread dies without closing the pipe and the call blocks
  forever. Covers `bytes`, `bytearray` and `memoryview`.
- `normalize_environment(env)` -- `None` stays `None`; everything else becomes
  a `str`-keyed dict, so a transport never has to guess at `Path` or `int`
  values.
- `capture_streams(capture_output, stdout, stderr)` -- resolve the three
  spellings of "capture" into one `(stdout, stderr)` pair.
- `dispatch_output(stdout_target, stderr_target, stdout, stderr, *,
  encoding=None, errors=None)` -- route captured bytes to where the caller
  asked. `None` means `sys.stdout`/`sys.stderr`, NOT discard;
  `subprocess.DEVNULL` is the way to discard.
- `write_output(target, value, *, encoding=None, errors=None)` -- the single
  stream half of the same rule.
- `reject_stdin_conflict(input, stdin)` -- refuse `input=` and `stdin=`
  together, as `subprocess` does, rather than silently preferring one.
- `wants_text(text, encoding, errors)` -- one answer to "is this call in text
  mode", so `errors=` alone selects text on every transport.
- `expired(command, timeout, *, output=None, stderr=None, orphaned=False,
  pid=None, text=False)` -- build the one `subprocess.TimeoutExpired` payload
  every transport raises: `.orphaned` says whether the remote command is still
  running, `.pid` names it when known, and the output attributes are never
  `None`.
- `CommandLine(str)` -- marks a string that is already a finalized command
  line for the target's own parser, so the executor passes it through instead
  of quoting it again.

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

`Executor(command, *args, stdin=None, stdout=None, stderr=None, cwd=None,
env=None, capture_output=None, check=None, encoding=None, errors=None,
input=None, timeout=None, text=None, **options)` defines the shared
shell-agnostic option surface. `*args` is positional and is what
`ExecutorCapability.ARGS` exists for: argv reaches an `ARGS`-capable executor
as separate elements, while a non-`ARGS` executor never receives them at all,
because the shell flavour has already rendered them into `command`. `ExecutionOptions` is the corresponding total-false `TypedDict`;
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
`Exec(program, *args)` is the direct-execution marker for `Host.run()`: one
program plus argv, no shell layer. The program may be a `str`, `bytes`, or a
path — all reach the transport as text, so a bare name resolves through the
target's `PATH`. Direct execution replaces the whole call, so an `Exec`
combined with any other command raises. Everything else is an ordinary value:
a path is a value that stringifies wherever it appears, so `run(p1, p2)` is two
shell commands. `Exec` is a deliberately non-iterable frozen dataclass --
`ShellFlavour.command_text` dispatches structured commands on `Iterable`, and
an iterable marker would be quoted into an argv instead of taking the direct
branch; `command_text` raises if an `Exec` reaches it.
One platform exception, handled rather than inherited: Windows dispatches a
`.bat`/`.cmd` target through cmd.exe, so a local `Exec` of one is quoted with
cmd's rules instead of the C runtime's. Without that an argument could close
the quoting and run a second cmd command, and `%VAR%` expanded before the
batch file saw it.
`Shell.execute(command, *args)` keeps its own program-plus-argv signature and
wraps into an `Exec` only when dispatching to a host `run(*cmds)` and only when
argv values are present -- `Shell.run` renders every command into one script
and dispatches it with no args, and that script is shell text, not a program.

## Configuration and lifecycle

- `Host(connection_string, **secrets) -> Host` dispatches a secret-safe URI
  through config implementations and the `hostctl.configs` entry-point group.
- `HostConfig(connection_string, **secrets) -> HostConfig` performs the same
  dispatch without creating a host. `str(config)` is its canonical,
  secret-free connection string and can be passed back to `HostConfig`.
- A `scheme://user:password@host` URI is valid *input*: the password is
  extracted into the credential arguments and stripped from the parsed
  authority, so it never reaches a field that `connection_uri` or `repr()`
  renders. Supplying a password both in the URI and as an argument raises.
  `redact_uri(uri)` STRIPS a password and returns a valid, reusable URI (not a
  masked one, so a rendered form can never round-trip a wrong credential), for
  logs, reprs, and error messages.
- A connection URI may carry a raw tab, CR, or LF in its **userinfo**: those
  are percent-encoded before `urlsplit` sees them, which would otherwise
  delete them silently (`ssh://u:pw<LF>otp:1@host` would authenticate with
  `pwotp:1`). So the credential-extras separator can be written naturally.
  A control character in the **host** is REJECTED -- deletion there rewrites
  the target (`ssh://host<LF>.other.example/` would resolve to
  `host.other.example`), and no encoding makes it meaningful.
  `redact_uri` never raises on either -- it is for diagnostics.
- `parse_credentials(password) -> (password, extras)` splits a password field
  on a newline: the first line is the password, each later line is a
  credential extra. `name:value` sets a value, a bare `name` is a flag
  equivalent to `name:` (empty string). Names are casefolded and stripped,
  values keep everything after the first `:`, blank lines are ignored, and
  CRLF is handled. URI dispatch runs the password field through it, so an OTP
  or other second factor travels in the same field; an extra a config does not
  declare is rejected by name rather than dropped.
- `config.connection_uri` never includes passwords or private keys;
  `config.scheme` matches its URI scheme.
- `with config as host:` and `with config.open() as host:` connect and always
  close. Re-entering the same active config raises `RuntimeError`.
- External config implementations declare `schemes=(...)`; registry hooks and
  caches are protected implementation details.

`LocalConfig`, `SshConfig`, `WinRMConfig`, and `ContainerConfig` produce their
corresponding hosts. `PosixConfig`, `WindowsConfig` and `IosConfig` compose a
system host from provider descriptors instead of owning a transport;
`IosConfig` produces an `IosHost`, which is session/command-only -- it has no
path grammar, so `path()` raises.

`ConnectionString(value, *, scheme=None, port=None, ...)` is the parsed form
of a target -- host, scheme, port, user, password -- with `is_local` answered
by `netimps` rather than guessed. It exists so a caller never reimplements
connection-string parsing. `uri_hostname(value)` returns the bare hostname of
a URI authority, unbracketing an IPv6 literal; it is the sibling of
`uri_host`, `redact_uri` and `parse_credentials`.

`__version__` is the installed version string.

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
- `env` is additive on every transport, including the local one: values are
  merged over the inherited environment, never replacing it. Remote flavours
  embed `export`/`$env:`/`set -gx` assignments; the local executor merges over
  `os.environ` rather than handing `subprocess.run(env=...)` a replacing map,
  which is what made the same call drop PATH locally and keep it over SSH.
  A genuinely empty environment remains a separate future contract -- and a
  dangerous one: a replacing environment without PATH/SystemRoot stops
  `powershell.exe` from starting at all.
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
- A custom `HostConfig` should declare `uri_credentials = ("password", ...)`.
  Dispatch then rejects any other credential BEFORE construction, so a typo
  (`passwrd=`) fails loudly instead of silently building a config with no
  password. `None` (default) skips the check for configs that validate
  themselves. `strict_uri_credentials`, `strict_uri_query`, and `uri_host`
  are public for configs writing `_from_parsed_uri` by hand.
- `ssh_providers(SshConfig)` and `winrm_providers(WinRMConfig)` return an
  `(executor_provider, path_provider)` pair sharing ONE transport, for
  composing a transport into a host you assemble yourself rather than taking
  the finished host `_create_host()` builds. They return a pair instead of
  exposing the transport because both providers must share it -- building them
  separately silently opens two connections, only one of which is closed.
- `input=` is normalized to the stream mode every executor is about to use, by
  `executor/_common.normalize_input`. Under a text mode (`encoding`/`errors`/
  `text`) bytes are decoded; under a binary mode str is encoded. This is not
  cosmetic: handing bytes to a text-mode `subprocess` stdin kills its writer
  thread, and the call then blocks forever because the child never sees EOF --
  `timeout=` does not fire. All executors share the helper so the same call
  behaves identically whichever provider a `SystemHost` selects.
- Output is `str` when ANY of `text`, `encoding`, or `errors` is given, and
  `bytes` otherwise -- `subprocess.run`'s rule, decided by the exported
  `wants_text(text, encoding, errors)`. `text=False` does not veto an
  `encoding`. An executor implemented outside hostctl must use the same
  helper, for the same reason as `normalize_input`: a `SystemHost` picks the
  provider, so a caller cannot write one correct invocation if providers
  disagree about the result type.
- `env` on `run`/`session`/`configure` accepts `EnvironmentSelection`: a
  mapping merges over the shell's default per key, the default empty mapping
  merges nothing and inherits it, and `None` runs without the shell's
  configured environment -- keeping whatever the host provides on its own,
  since nothing is sent to override it. `None` does NOT mean an empty
  environment. Declining `env` does not affect `cwd`.
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

## Provider contract

These are the names you implement when you add a transport, and the rules
hostctl applies to whatever you return.

- `ExecutorProvider(name, executor, *, capabilities=None, probe=None)` and
  `PathProvider(name, factory, *, capabilities=None, probe=None)` are the two
  provider shapes. `name` is what a trace and a `.via()` pin refer to;
  `executor` is any callable matching the `Executor` protocol, and `factory`
  builds a `pathlib_next.Path` from segments. `capabilities` is a set of
  operation names -- strings, so an `ExecutorCapability` member and its
  spelling are interchangeable. `PathProvider.DEFAULT_CAPABILITIES` is the
  full filesystem vocabulary: `stat`, `scandir`, `open`, `open_read`,
  `open_write`, `read`, `write`, `exists`, `is_file`, `is_dir`, `mkdir`,
  `chmod`, `unlink`, `rmdir`, `rename`, `symlink_to`, `readlink`.
- `ProviderProbe(availability, reason="", capabilities=frozenset(),
  system_hint=None)` is what `probe()` returns: `"available"`, `"degraded"`
  or `"unavailable"`. `usable` is true for the first two. A probe may narrow
  the declared capabilities; it must not invent any.
- `OperationNotStarted(reason, *, cause=None)` is the **only** way to decline
  a provider for the generation, and it means exactly one thing: nothing was
  sent, so the operation may be retried on the next provider. Raise it before
  dispatch or not at all -- a possibly-started operation is never replayed.
  A `NotImplementedError` from an operation that cannot mutate before raising
  falls through to the next provider for **that call only**; it does not take
  the provider out of service.
- `ProviderSelector` holds the ordered providers and the per-generation
  declines; `ProviderSelection` is one resolved choice.
  `ProviderSelector.redact(value)` is the one redaction used in traces --
  rendered commands routinely carry credentials, so log through it.
- `SessionInitializer` is the hook a provider may accept to prepare a session
  (a login, a `cd`, an environment) once per connection rather than per call.
- `CompositePosixPath` / `CompositeWindowsPath` are the path types a composed
  host returns. They preserve the logical path, select a provider per
  operation, pin mutations and streams to the provider that started them, and
  answer `pathlib_next`'s `_same_filesystem()` with the host's provider set,
  so a cross-host copy is never mistaken for a file copied onto itself.
  `path.via(name)` returns a path pinned to one provider.

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
- A timeout raises `subprocess.TimeoutExpired` carrying the SAME payload on
  every transport: `.orphaned` (a bool -- `True` when the transport could not
  stop the command, so it is still running there), `.pid` (`None` unless the
  transport knows one), and `.output`/`.stderr` as `b""`/`""` rather than
  `None`. Build one with `executor.expired(...)` rather than
  `subprocess.TimeoutExpired(...)`, so a caller's
  `if exc.orphaned: alert(exc.pid)` cannot raise `AttributeError` depending
  on which provider answered.
- A usable `run()` returns `subprocess.CompletedProcess`. **`check` defaults
  to `True` and `capture_output` to `True`** -- both the opposite of
  `subprocess.run`, deliberately: a remote command that fails is an error by
  default, and its output is captured rather than inherited. Pass
  `check=False` to inspect `returncode` instead. Command timeouts raise
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
whether a process/channel termination hook was available.
A composed SSH host opens **two** connections and authenticates twice: the
exec leg is hostctl's own, the path leg is `pathlib_next`'s SFTP backend,
which takes connect options rather than a live connection. `bufsize` is
asyncssh's stdin CHUNK size here, not `subprocess`'s buffering policy, so
values below 512 are refused rather than obeyed. `run()` buffers the whole
response in memory even when `stdout=` names a sink, and a file-object
`stdin=` is read fully before dispatch; both are memory characteristics of
the buffered contract, not of the channel.
An SSH host is **not fork-safe** in the sense of inheriting a usable
connection: a child notices the loop it belongs to has changed and drops the
inherited connection without closing it (closing would tear down a socket the
parent still uses), then reconnects on its own. `dialect="auto"`
retains the executable path reported by the successful probe. Persistent SSH
process reads, writes, EOF, and close operations use the same transport-error
normalization as `wait()`.

## Containers

`ContainerConfig(container, engine_url=None, user=None, workdir=None,
executable=None, dialect="auto", path_flavor="auto", client_factory=None)` uses the optional
`container` extra and Docker Engine API. Inspection selects Linux/POSIX or
Windows/PowerShell semantics. `ContainerHost` supports buffered exec,
persistent sessions/TTYs, and archive-backed POSIX or Windows paths. Archive
paths support stat/traversal/read/write/append/exclusive-create, plus
`symlink_to()`/`readlink()` -- a `SYMTYPE` tar member is a faithful
representation; archive-only mkdir/remove/rename/chmod raise
`NotImplementedError`. `run()` takes no stdin and no `timeout=` (Docker's
buffered exec streams neither, and the exec API has no cancellable deadline).
`iterdir()`/`walk()` pull one archive per directory, so a deep tree is
re-downloaded level by level -- list a large one through `run()` instead.

## QEMU Guest Agent

`QemuConfig(domain, transport="libvirt", connection=None, socket_path=None,
ssh=None, agent_timeout=10.0, max_reply_size=48*1024*1024, dialect="auto",
path_flavor="auto", transport_factory=None, serial_console=None,
path_helper=None)` creates a
`QemuHost`. Transports are local libvirt (`qemu-libvirt` extra), direct Unix
socket, or an AsyncSSH-tunneled remote Unix socket. Discovery positively probes
QGA and its enabled command list.

`QemuExecutor` uses buffered `guest-exec`/`guest-exec-status`. It declares
argv only: `guest-exec`'s `env` list is applied as the guest's whole
environment rather than added to it, so the host embeds assignments in the
rendered script and a direct `Exec` refuses `cwd=`/`env=`. QGA cannot cancel
timed-out processes; `TimeoutExpired.orphaned` is true and `.pid` is retained
when known. `QgaPathBackend` uses bounded file-handle RPCs; `exists()` is
answered from those RPCs alone, while metadata and namespace mutations need a
guest helper (`QemuConfig(path_helper=...)`) and raise `NotImplementedError`
without one.

`SerialHost.run()` frames each command as its own profile exchange and
refuses a structured (argv) command: the host names no shell flavour, so it has
no quoting rule to apply. `stdin=`, `bufsize=` and `input=` all raise.
`spawn()` reads `bytes` unless `encoding=`/`errors=` asks for text. A failed
login raises `ConsoleProtocolError`, never a bare `TimeoutError`, and a
transcript attached to an error has `secret=True` login values replaced with
`<redacted>`. Negotiation is cached against the transport it ran on, so
`host.executor.close()` forces a fresh login. `SerialConfig` takes no
`username`/`password`: console credentials live in the profile's `login=`
steps.

`QemuConfig.max_reply_size` (default 48 MiB) bounds one QGA reply, and
`agent_timeout` bounds one round trip independently of a command's `timeout=`:
an agent that stops answering raises `ConnectionError`, not a command timeout.
`run()` results carry `stdout_truncated`/`stderr_truncated` and warn when set.
`dialect="auto"`/`path_flavor="auto"` require positive evidence of the guest
family (`guest-get-osinfo`, or a family-exclusive command in `guest-info`) and
raise rather than defaulting to POSIX. `info().os_family` is a family from
`kernel-name`, not the os-release `id`. A `QgaPathBackend` is invalidated by
`QemuHost.close()`, so a path handed out earlier refuses instead of
reconnecting. `open("rb")` is seekable where `guest-file-seek` is advertised;
the exclusive `x` modes are refused by `open()` without a guest helper.

`QgaCommandError` is a guest agent error reply, carrying `.error_class` and
`.description`; a path operation translates it into the matching `OSError`
(`FileNotFoundError`, `PermissionError`, ...). `QgaProtocolError` is a
malformed or oversized reply and is itself a `ConnectionError`.
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
remoting; explicit credentials use pywinrm. On that native path
`server_cert_validation="ignore"` becomes `New-PSSessionOption -SkipCACheck
-SkipCNCheck` (both checks, matching pywinrm), and the remote exit code is
carried back explicitly, so a native command failing only by status is
reported as a failure rather than as success. `WinRMPath.open("rb")` fetches
bounded ranges; writable modes stage content and transfer Base64 chunks on
close. WinRM stdin and command
deadlines remain unsupported. Transport timeouts are not a total command
deadline. pywinrm Session has no guaranteed close API; hostctl calls `close()`
only when a provided session exposes it.
`pypsrp_available()` reports whether the optional PSRP dependency imports;
`require_pypsrp()` raises the install hint when it does not. PSRP runspaces
are exposed separately through the WinRM transport provider and
`RunspaceSession.invoke()`. They retain typed PowerShell streams and state, and
are not advertised as a byte-oriented `spawn`/TTY process.

## Local and utilities

`LocalHost.path()` and `LocalHost.run()` work on POSIX and Windows.
`LocalExecutor` provides native argv, cwd, environment, stream, encoding,
check, and timeout behavior through `subprocess.run`.

`SerialConfig(port, baudrate=115200, bytesize=8, parity="N", stopbits=1,
xonxoff=False, rtscts=False, dsrdtr=False, read_timeout=0.1, write_timeout=10,
inter_byte_timeout=None, exclusive=None, protocol=RawConsoleProfile(),
serial_factory=None, serial_port=None)` takes the DEVICE as its first
argument, the way pyserial names one (`"COM3"`, `"/dev/ttyUSB0"`,
`"rfc2217://host:4001"`); the `serial:///...` URI is the dispatcher's
spelling, `Host("serial:///COM3")`. `serial_factory` and `serial_port` are
supported injection seams, not test-only: a caller that already owns a port
passes `serial_port=` and keeps ownership of it.

`SerialConfig`/`SerialHost` provide an opaque `serial:///...` URI and one
exclusive byte-stream lease. `RawConsoleProfile` supports sessions only;
`PromptConsoleProfile` adds bounded login/prompt framing and advertises
`run` only when `reliable_status=True` and a completion marker is configured.
Streams are merged, PTY/path/status semantics are absent unless the profile
explicitly supplies them. Optional PySerial support is the `serial` extra and
injected serial objects remain caller-owned. Break, DTR, and RTS are available
on `SerialProcess`/`SerialConsoleProcess`; RFC 2217 URLs are not encrypted.
`SerialConsoleProtocol` is the profile contract, `LoginStep` one expect/send
pair of a login sequence, and `ConsoleProtocolError` what a profile raises
when the console does not answer the way the protocol requires.

`TerminalOptions(term, columns, rows, pixel_width, pixel_height)` is the
resolved terminal request `spawn(terminal=...)` produces.
