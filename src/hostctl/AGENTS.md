# hostctl — API header

Protocol-independent host execution and filesystem paths. Core requires
`pathapi`; transport integrations are optional extras.

Every config takes its target positionally and everything else by keyword: a
second positional argument raises `TypeError`, so an SSH port is always
`port=2222` (a type checker still reads the dataclass's own `__init__`).
`has_ssh()` and `has_psrp()` report whether the `ssh` extra (asyncssh and
sftppath) and the `psrp` extra (pypsrp, on Python 3.10+) are installed,
importing nothing; a missing extra otherwise fails where it is used, naming
it.

The `hostctl` command line is `hostctl.cli` and needs the `cli` extra; see
"Command line" at the end. `import hostctl` never imports it.

`hostctl.__all__` is the stable surface: hosts and configs you construct,
exceptions you catch, types you annotate with, and the provider/shell contracts
you implement. Every module whose path has no leading underscore is public and
declares `__all__`; a name outside it, and every `_`-prefixed module, is
internal and may change without notice. Concrete executors, process adapters,
path backends and backend path classes are internal: a host hands them back
typed as `pathapi.Path`, `Process` or `Executor`. `LocalExecutor` is the one
exported executor, because it is the one a caller constructs directly, to
compose a host out of providers without a transport.

Where names are imported from (each is also re-exported from `hostctl` when
the root lists it):

- `hostctl.host` -- `Host`, `HostConfig`, `HostInfo`, and the selection aliases
  every config takes: `ShellSelection` (a shell flavour, its class or name, or
  `"auto"`) for `shell=`, and `PathnameSelection` (a pure-path or
  `pathapi.Pathname` class, `"posix"`, `"windows"` or `"auto"`) for
  `pathname=`.
- `hostctl.exceptions` -- the exception tree (see "Exceptions").
- `hostctl.uri` -- `ConnectionString`, `parse_credentials`, `redact_uri`,
  `strict_uri_credentials`, `strict_uri_query`, `uri_host`, `uri_hostname`.
- `hostctl.system` -- `SystemConfig`, `SystemHost`, `PosixConfig`, `PosixHost`,
  `WindowsConfig`, `WindowsHost`, `IOSConfig`, `IOSHost`,
  `register_system_provider`, `system_provider_names`.
- `hostctl.provider` -- `ExecutorProvider`, `PathProvider`, `ProviderProbe`,
  `ProviderSelection`, `ProviderSelector`, `SessionInitializer`, and
  `READ_ONLY_PATH_OPERATIONS`, the capability set of a content-only endpoint.
- `hostctl.executor` -- the `Executor` protocol, `ExecutorCapability`,
  `ExecutionOptions`, `CommandLine`, the option aliases `CommandLike`,
  `CommandArgument`, `CaptureOutput`, `StreamTarget`, `CommandInput`, `StrPath`,
  `CommandEnvironment`, and the stream helpers listed below.
- `hostctl.process` -- `Process`, `ProcessData` (`str | bytes`),
  `TerminalOptions`, `TerminalRequest` (what `spawn(terminal=)` accepts) and
  `terminal_options()`, which resolves one into the other.
- `hostctl.paths` -- `CompositePosixPath`, `CompositeWindowsPath`.
- `hostctl.shell` -- `Shell`, `ShellSession`, `ShellFlavour`, `ShellOperator`,
  `ShellTarget`, `ShellCommand`, `ShellToken` (one rendered token),
  `ShellFlavourSelection` (a flavour, its class, or its registered name), the
  command markers `Exec`, `Cmdlet`, `PSParam`, the flavour classes
  `PosixShellFlavour`, `BashShellFlavour`, `ZshShellFlavour`,
  `FishShellFlavour`, `CmdShellFlavour`, `PowerShellFlavour` with their
  instances `POSIX_SHELL`, `BASH`, `ZSH`, `FISH`, `CMD`, `POWERSHELL`, `PWSH`,
  and `register_shell_flavour`, `shell_flavour`, `shell_flavour_names`,
  `wants_epilogue`.
- `hostctl.sync` -- `ProgressReader`, `host_checksum`, `stat_checksum`.
- `hostctl.transports.local` -- `LocalConfig`, `LocalHost`, `LocalExecutor`,
  `local_providers`.
- `hostctl.transports.ssh` -- `SSHConfig`, `ssh_providers`, and the argument
  aliases `SSHKeySource` (a key path, bytes or text), `SSHClientKeys` and
  `SSHKnownHosts` (one source or a sequence of them).
- `hostctl.transports.winrm` -- `WinRMConfig`, `winrm_providers`, and the PSRP
  results `RunspaceSession`, `PipelineResult`, `PipelineStreams`.
- `hostctl.transports.container` -- `ContainerConfig`, `ContainerHost`.
- `hostctl.transports.qemu` -- `QEMUConfig`, `QEMUHost`, `QEMUTransport` (the
  guest-agent transport name), the `GuestAgentTransport` and `GuestPathHelper`
  protocols, `PosixGuestPathHelper`, `WindowsGuestPathHelper`,
  `QEMUSerialConsole`.
- `hostctl.transports.serial` -- `SerialConfig`, `SerialHost`, `SerialProcess`,
  `SerialLike` (the pyserial-shaped port protocol), `SerialFactory`, and the
  console profiles `ConsoleProfile`, `RawConsoleProfile`,
  `PromptConsoleProfile`, `LoginStep`.
- `hostctl.cli` -- `main`, with one subcommand class per module
  (`hostctl.cli.cat.Cat`, `.cp.Cp`, `.info.Info`, `.ls.Ls`, `.run.Run`,
  `.shell.Shell`).

The transport packages need their extras; importing one whose dependency is
missing works, and the first use names the extra to install.

`login_shell` names what parses the command string when it ARRIVES. AsyncSSH
sends one string and the server hands it to a login shell: on POSIX that is
`$SHELL -c`, exactly what this package renders for, but Windows OpenSSH's stock
server hands it to `cmd.exe /c`, so a `cmd`-dialect command line is parsed by
cmd TWICE. `"auto"` reads the dialect (a Windows path flavour means the stock
`cmd.exe`); name it explicitly when the server's `DefaultShell` has been
changed. `ShellFlavour.escape_for_one_parse(text)` is the outer layer this
selects, implemented by `cmd` and refused by default rather than guessed.
`"posix"` covers every sh-family shell and fish; csh and tcsh are unsupported.
`run()` and `spawn()` both render through `render_command(...,
login_shell=...)`, so they cannot disagree.

A shell flavour answers TWO quoting questions, not one. `quote(value)` is
syntactic -- enough that this shell sees one word and expands nothing.
`argument(value, *, target=ShellTarget...)` quotes for whatever parses the token
NEXT: `SHELL` (a cmd builtin, split by cmd itself), `NATIVE` (a child's own argv
parser -- the C runtime on Windows, nothing on POSIX), `CMDLET` (PowerShell's
binder), `PROGRAM` (the program slot, read by `CreateProcess`), `BATCH` (a batch
file's `%*`/`%1`, which cmd parses again) or `UNRESOLVED` (a program that may
turn out to be a batch file). Where the layers coincide -- every POSIX-family
shell, since `execve` takes a vector -- the default `argument()` is just
`quote()`. `command_target(values)` classifies a Windows program by its name:
a cmd builtin is `SHELL`, `.exe`/`.com` is `NATIVE`, `.bat`/`.cmd` is `BATCH`,
anything else (a bare name, `.ps1`) is `UNRESOLVED`. The FIRST element of a
structured command is the program and is never argv data: cmd quotes it like a
builtin operand, PowerShell as a literal. A `BATCH` argument is one token
(`"` + body + `"`, `"` -> `""`, `%` -> `%%cd:~,%`): a Microsoft C runtime reads
`""` as one quote, a `CommandLineToArgvW` consumer (node, most system tools)
may split a value with whitespace after a quote, and none executes it. An
`UNRESOLVED` argument raises `ValueError` for a character a batch re-parse
would act on (`"` on cmd; `" & | < >` on PowerShell); `%`, `^`, `!` corrupt but
cannot start a command. Windows PowerShell 5 raises for a native argument it
would split (whitespace only after an odd count of quotes); `PWSH` and `Exec`
carry those. PowerShell cannot tell a cmdlet from a program (that needs a
runspace), so `Cmdlet(name, *args)` is the cmdlet spelling -- PowerShell
literals, no C-runtime escaping, nothing refused -- and `PSParam("-Name")` is
the one token rendered bare. A plain string is always a value; there is no
`-Name` inference. Other shells raise `TypeError` for either marker.

`cwd_guard` declares how a failed `cd` is stopped from running the payload:
`"operator"` fuses the change onto a GROUPED payload with the AND operator,
`"statement"` is for a flavour whose change-directory statement aborts the
script itself (PowerShell's `-ErrorAction Stop`). It is declared rather than
inferred from `context_order`'s ordering, which puts PowerShell's `env`
between `cwd` and `command`.

`hostctl.executor`'s public helpers are the ones a transport adapter needs to
behave like the others; an executor that skips them is where cross-transport
divergence comes from:

- `normalize_input(input, *, text_mode, encoding=None, errors=None)` -- match
  `input` to the stream mode about to be used. A mismatch is not a clean
  error: the writer thread dies without closing the pipe and the call blocks
  forever. Covers `bytes`, `bytearray` and `memoryview`; `CommandInput` is those and
  `str`, and anything else (an `int`, say) raises `TypeError`.
- `normalize_environment(env)` -- `None` stays `None`; everything else becomes
  a `str`-keyed dict, so a transport never has to guess at `Path` or `int`
  values. Names are checked first by `validate_environment_names(env)`, which
  raises `ValueError("invalid environment variable name: ...")` for a name
  outside `[A-Za-z_][A-Za-z0-9_]*`; `SystemHost.run()` calls it before it
  selects a provider, so the same call is valid or not on every transport.
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
`pathapi.sync.PathSyncer` and the existing path copy machinery.

`SystemHost`, `PosixHost`, and `WindowsHost` compose ordered executor/path
providers. Opening an `SSHConfig` or `WinRMConfig` (`config.open()`) returns a
`PosixHost` or `WindowsHost` that retains the original connection URI
and lifecycle while exposing transport operations through provider adapters;
transport implementations remain private.
`register_system_provider(name, resolver, *, replace=False)` extends logical
system URI descriptors; `resolver(config, kind)` returns the provider for
`kind` (`"executor"` or `"path"`) or `None`. A name already registered raises
`ValueError` unless `replace=True`, and an unknown descriptor loads the
`hostctl.system_providers` entry point of that name. Built-ins include
`local`, `ssh`, `sftp`, and `winrm`; transport descriptors require matching
objects in `SystemConfig(provider_options=...)` and never serialize
credentials into the canonical URI. `system_provider_names()` and
`host_schemes()` return sorted tuples of what is registered, entry-point names
included without importing them.

`Executor(command, *args, stdin=None, stdout=None, stderr=None, cwd=None,
env=None, capture_output=None, check=None, encoding=None, errors=None,
input=None, timeout=None, text=None, **options)` defines the shared
shell-agnostic option surface. `*args` is positional and is what
`ExecutorCapability.ARGS` exists for: argv reaches an `ARGS`-capable executor
as separate elements, while a non-`ARGS` executor never receives them at all,
because the shell flavour has already rendered them into `command`. `ExecutionOptions` is the corresponding total-false `TypedDict`;
executor-specific extensions remain keyword options.
`CommandLike` is `str | pathlib.PurePath | pathapi.Pathname`; paths
remain path objects until the concrete executor converts them for transport.
`ExecutorCapability.ARGS`, `.CWD`, and `.ENV` declare native executor support.
`SSHExecutor` and `WinRMExecutor` inherit the `Executor` protocol so shared
protocol implementations can be added once. `Shell` is NOT an `Executor`:
calling a `Shell` configures it (`shell(cwd=..., env=...)`), and a positional
argument raises `TypeError` -- run a program with argv through
`host.executor(command, *args)` or `host.run(Exec(command, *args))`.
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
cmd's rules instead of the C runtime's: the program in plain quotes (a `"` or
`%` in its path raises `ValueError`), every argument as a batch token (see
`ShellTarget.BATCH`). Without that an argument could close the quoting and run
a second cmd command, and `%VAR%` expanded before the batch file saw it.
`Cmdlet(name, *args)` and `PSParam(name)` (exported by `hostctl`,
`hostctl.host` and `hostctl.shell`) are the PowerShell-only command markers, as
non-iterable as `Exec`. After `REDIRECT`/`APPEND` the operand is a file name
(`ShellFlavour.redirect_operand`): raw text stays source, a path or one-element
tuple is quoted for the shell itself. Windows PowerShell 5 writes such a name
with `| Out-File -LiteralPath NAME -Encoding Unicode [-Append]` (the bytes `>`
wrote), so `[`/`]` in it are literal; pwsh keeps `>`, whose cmdlet output still
reads them as a wildcard. On PowerShell 5 an absolute program path containing
`[ ] * ?` is backtick-escaped after `&`. A path given as a whole command renders
as the one-element command `(path,)`. Under `cmd`, a structured `start` always
renders `start ""` and a structured `call` raises `ValueError`.

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
  logs, reprs, and error messages. A WELL-FORMED URI IS NEVER OVER-REDACTED:
  when it parses, `urlsplit`'s authority is the authority, so an `@` elsewhere
  (`ssh://nas:22/mail/user@example.com`) is left alone. Only input that does
  not parse gets the textual last-`@` guess, which may over-redact -- by then
  the text has already failed to say where its authority ended.
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
- A config constructor takes a host and refuses a URI (`ValueError` naming
  `HostConfig(uri)`, password redacted). A URI refuses a C0, DEL or C1 control
  character in its host or userinfo, raw or percent-encoded.
  `SystemConfig(authority, *, shell, executor, path, provider_options,
  initializer)` takes `executor=`/`path=` as sequences of names (a bare string
  is a `TypeError`) and no other keywords; a `SystemHost` takes a `HostConfig`,
  never a connection string.
- `with config as host:` and `with config.open() as host:` connect and always
  close. Re-entering the same active config raises `RuntimeError`.
- Implementing a config: `class MyConfig(HostConfig, schemes=("mine",))` with
  `uri_credentials = ("password", ...)`;
  `@classmethod _from_parsed_uri(cls, parsed, **credentials) -> MyConfig`
  builds it from a `urlsplit` result (use `strict_uri_query`,
  `strict_uri_credentials`, `uri_host`); `_create_host(self) -> Host` returns
  the host, which callers reach through `config.open()` or `with config as
  host`. The single underscore means "override in a subclass, never call";
  there is no `from_uri`. A narrowed parse is `config = HostConfig(uri);
  isinstance(config, SSHConfig)`. Registry state (`_uri_schemes`, caches) stays
  private.
- External config implementations declare `schemes=(...)`. A scheme another live config
  already claims raises `ValueError("host scheme 'x' is already registered by
  Name")` at class definition; redefining the same module-qualified class
  replaces it, and `replace=True` takes the scheme over. One plugin that fails
  to import is warned about and skipped, so it does not break dispatch of
  other schemes; its own scheme raises `RuntimeError` from the import error.

`LocalConfig`, `SSHConfig`, `WinRMConfig`, and `ContainerConfig` produce their
corresponding hosts. `PosixConfig`, `WindowsConfig` and `IOSConfig` compose a
system host from provider descriptors instead of owning a transport;
`IOSConfig` produces an `IOSHost`, which is session/command-only -- it has no
path grammar, so `path()` raises.

`ConnectionString(value, *, scheme=None, port=None, ...)` is the parsed form
of a target -- host, scheme, port, user, password -- with `is_local` answered
by `netimps` rather than guessed. It exists so a caller never reimplements
connection-string parsing. `import hostctl` changes no stdlib module and does
not import `netimps`; the first call that needs it (a `ConnectionString`, a
default port) does, and netimps then adds its `socket`/`os` shims on Windows
unless `NETIMPS_SOCKET_PATCH=0` is set. A bare host (`nas`) or `host:port` (`nas:8443`)
needs a `scheme=` or `defaults={"scheme": ...}` and raises "no scheme"
without one; a full URI carries its own. The value MUST otherwise be a valid
URI: an unclosed IPv6 literal, a non-numeric port, or an unescaped `/`, `?` or
`#` in a password RAISES. An opaque URI with no host (`local:`,
`serial:COM3`) parses, with an empty host that `is_local` calls local.
**Percent-encode reserved characters in a password** -- there is no lenient
mode and none will be added, because a password may itself contain `@` and
`:`, so nothing can recover where an unescaped one ended, and guessing would
be guessing what to connect to. (`@` and `:` do parse: last `@`, first `:`,
per the URI grammar.) Use `redact_uri` when the text may not parse at all and
the job is only keeping a secret out of a log. The error for an unencoded
`/`, `?` or `#` in a URI password (`HostConfig(uri)`, `Host(uri)`,
`ConnectionString`) is a fixed message that never echoes the URI or any part
of the password.

`uri_hostname(value)` returns the bare hostname of a URI authority,
unbracketing an IPv6 literal; it is the sibling of `uri_host`, `redact_uri`
and `parse_credentials`.

`__version__` is the installed version string.

## Shell contract

- `Shell(flavour, executor)` binds a `ShellFlavour` to a host (an object with
  `run`, not callable) or to an executor (a callable). Over a host,
  `Shell.run(x)` IS `host.run(x)` -- the host renders one shell layer with its
  provider's capabilities -- and the flavour must be the host's own
  (`ValueError` otherwise). Over an executor, `Shell.run()` renders a script
  and sends it by the executor's capabilities: `args` gets the flavour's
  invocation as argv (a `CommandLine` for cmd), otherwise the script string;
  `cwd`/`env` are forwarded when the executor carries them natively and
  embedded in the script when not. Subprocess options (`stdin`, `stdout`,
  `stderr`, `capture_output`, `check`, `encoding`, `errors`, `input`,
  `timeout`, `text`) pass through to the executor or host. `flavour`, `cwd`,
  `env`, `encoding` and `errors` are read-only properties.
- `ShellFlavour.script(cmds, *, cwd=None, env=None, epilogue=True) -> str`
  constructs a script in one target shell language. `epilogue=False` leaves
  off the status epilogue; `hostctl.shell.wants_epilogue(capabilities)` is
  `False` for a provider declaring `manages_status` and is how every caller
  decides.
- `ShellFlavour.environment_script(env) -> str` is a reusable standalone
  environment-mapping renderer. The base normalizes/validates variable names
  and joins each flavour's `environment_assignment(key, value)` output.
  Values remain objects so a flavour can preserve meaningful types; built-ins
  decode bytes and stringify ordinary values.
- `ShellOperator.PIPE`, `.AND`, `.OR`, `.REDIRECT`, `.APPEND`, and `.SEQUENCE`
  are explicit infix tokens between top-level commands. Flavours own their
  spelling and may reject operators they cannot represent portably.
  `ShellFlavour.redirect(value, target, operand) -> str` renders a
  `REDIRECT`/`APPEND` with its target (`operand` is `redirect_operand(target)`);
  the default is `operator(value) + operand`.
- Raw strings stay verbatim; tuple/list commands quote each item as data;
  standalone and structured paths are quoted; top-level commands otherwise
  use the flavour's `command_separator`.
- `ShellFlavour.command(cmds, *, executable=None, cwd=None, env=None,
  login_shell=None) -> ShellCommand` wraps that script for an SSH exec channel.
  `login_shell` names what parses the string first (`None`: nothing, as with
  `CreateProcess`; `"posix"`: an sh-family shell or fish; `"cmd"`;
  `"powershell"`). POSIX and fish join with `login_join`, which every
  sh-family shell and fish read back verbatim, with `cwd` inside the script;
  `cmd` adds the second escape for `"cmd"`. A custom flavour must accept the
  keyword to be used over SSH.
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
- `shell_flavour_names()` returns the sorted names `shell_flavour()` accepts,
  plugin entry points included.
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
- `ssh_providers(SSHConfig)` and `winrm_providers(WinRMConfig)` return an
  `(executor_provider, path_provider)` pair sharing ONE transport, for
  composing a transport into a host you assemble yourself rather than taking
  the finished host `_create_host()` builds. They return a pair instead of
  exposing the transport because both providers must share it -- building them
  separately silently opens two connections, only one of which is closed.
  `local_providers()` returns the local pair: no transport to share, it is the
  pair for a local leg.
- `input=` is normalized to the stream mode every executor is about to use, by
  `hostctl.executor.normalize_input`. Under a text mode (`encoding`/`errors`/
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
  builds a `pathapi.Path` from segments. `capabilities` is a set of
  operation names -- strings, so an `ExecutorCapability` member and its
  spelling are interchangeable. `PathProvider.DEFAULT_CAPABILITIES` is the
  full filesystem vocabulary: `stat`, `scandir`, `open`, `open_read`,
  `open_write`, `read`, `write`, `exists`, `is_file`, `is_dir`, `mkdir`,
  `chmod`, `unlink`, `rmdir`, `rename`, `symlink_to`, `readlink`, `utime`,
  `hardlink_to`.
- `ProviderProbe(availability, reason="", capabilities=frozenset(),
  system_hint=None)` is what `probe()` returns: `"available"`, `"degraded"`
  or `"unavailable"`. `usable` is true for the first two. A probe may narrow
  the declared capabilities; it must not invent any.
- `OperationNotStartedError(reason, *, cause=None)` is the **only** way to decline
  a provider for the generation, and it means exactly one thing: nothing was
  sent, so the operation may be retried on the next provider. Raise it before
  dispatch or not at all -- a possibly-started operation is never replayed.
  A `NotImplementedError` from an operation that cannot mutate before raising
  falls through to the next provider for **that call only**; it does not take
  the provider out of service.
- `ProviderSelector` holds the ordered providers and the per-generation
  declines; `ProviderSelection` is one resolved choice (`provider`, `trace`,
  `generation`, `pinned`). `select(capability=, exclude=, pin=)` re-admits
  a decline recorded by an earlier operation only when no other provider can
  serve. `effective_capabilities(provider)` is what a provider can do now:
  its declared set intersected with what its probe states (the declared set
  when the probe states none), and `select`, `host.capabilities`,
  `executor_capabilities`, `provider_details` and a composed path's dispatch
  all read it. `supports()` on a composite stays on the declared set, which
  must not probe. Trace entries keep a constant `"policy": "ordered"`.
- `host.executor_providers` and `host.path_providers` are the ordered tuples a
  `SystemHost` was composed from. `host.replace_providers(*, executor=None,
  path=None)` swaps either set (`None` keeps it), closes each replaced
  transport once unless a kept provider shares it, and starts a new connection
  generation, so the session initializer runs again. Duplicate names raise
  before anything changes. Paths obtained earlier keep the old providers;
  obtain them again.
- Teardown: `close()` closes a provider's `transport` attribute when it has
  one (providers sharing a transport share the attribute, so it closes once),
  otherwise the provider's own `close()`.
- A provider's optional `connect()` is where a pre-dispatch failure belongs.
  Composite paths call a path provider's `connect()` before **every**
  dispatch, so it must be idempotent and cheap once connected.
  `ProviderSelector.redact(value)` is the one redaction used in traces --
  rendered commands routinely carry credentials, so log through it. It also
  masks fish `set -gx NAME value` and quoted values holding `'`. An `env=`
  value embedded in the command text is masked before rendering: the dispatch
  log, `CompletedProcess.args` and the `cmd` of `CalledProcessError`/
  `TimeoutExpired` carry `<redacted>` in its place (the sent command is
  unchanged).
- `SessionInitializer` is the hook a provider may accept to prepare a session
  (a login, a `cd`, an environment) once per connection rather than per call.
- `CompositePosixPath` / `CompositeWindowsPath` are the path types a composed
  host returns. They preserve the logical path, select a provider per
  operation, pin mutations and streams to the provider that started them, and
  answer `pathapi`'s `_same_filesystem()` with the host's provider set,
  so a cross-host copy is never mistaken for a file copied onto itself.
  `path.via(name)` returns a path pinned to one provider.
- `pathapi.protocols.supports(path, Protocol)` answers what a host path can
  do without a call that raises. A composite path says yes to `Stat`,
  `Listing`, `BinaryOpen`, `Create`, `Remove`, `Chmod`, `Readlink`,
  `Symlink` or `Move` when some provider it may route to (only the pinned one
  after `.via()`) declares the operations behind it (`stat`, `scandir`,
  `open_read`, `mkdir`, `unlink` and `rmdir`, `chmod`, `readlink`,
  `symlink_to`, `rename`; `path` declares all). `Utime`, `Hardlink`, `Owned`,
  `Resolve`, `NativeChecksum` and `Chown` also need the backend path class to
  implement them (`utime`, `hardlink_to`, `stat`, `stat`, `read`, `chmod`
  declared), so a composite over a path class without one says no. No provider
  is probed, and the answer is advisory. A container path has no `Create`, `Chmod`,
  `Remove` or `Move`; a QGA path has no `Readlink` or `Symlink`, and its
  metadata and namespace protocols follow the guest helper.

## Host contract

- `Host` is abstract. Base `run()` and `path()` raise `NotImplementedError`.
- Hosts expose delegated `scheme`/`connection_uri`, explicit `capabilities`,
  `info() -> HostInfo`, and `connect()`/`close()` plus context management.
- `host.shell_flavour` is the explicitly known target-shell strategy;
  `host.shell` builds `Shell(host.shell_flavour, host)`. SSH uses its configured
  or positively detected flavour, WinRM uses PowerShell, and local execution
  selects POSIX sh or Windows PowerShell from the local platform.
- SSH provider run renders through its shell flavour and delegates the finalized
  command to `SSHExecutor`; WinRM provider run delegates its finalized
  PowerShell script to `WinRMExecutor`.
- `HostInfo` fields are optional; unknown system values remain `None`.
- A usable `path()` returns a `pathapi.Path`.
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

`SSHConfig(host, port=22, username=None, password=None, client_keys=None,
executable=None, known_hosts=(), shell=POSIX_SHELL,
pathname=pathapi.PosixPathname, login_shell="auto", connect_timeout=30.0,
ssh_config=None, connect_options={})`.

`username=None` leaves the account to asyncssh (the local user, as `ssh` does);
there is no `root` default. `ssh_config` is the one way to read an ssh_config,
and both legs read it; `connect_options` are extra `asyncssh.connect` keywords
for both legs (a key hostctl owns -- `host`, `port`, `username`, `password`,
`client_keys`, `known_hosts`, `config`, `connect_timeout` -- raises and names the
field to use), kept out of the URI and `repr()`. `ssh_config` is kept out of
the URI too, and an `ssh://` URI naming it raises: a `ProxyCommand` starts a
local process, which a URI's author (an MCP tool caller, say) must not choose.

Authentication fields are explicit and excluded from repr. `shell` selects
POSIX or PowerShell command construction independently of the POSIX/Windows
SFTP path flavor. `shell` is a `ShellFlavour` strategy. `pathname` is `"posix"`,
`"windows"`, `"auto"` or a concrete `pathapi.Pathname` or `pathlib.PurePath`
subclass; `"auto"` connects while the host is created and reads the SFTP login
directory (`/C:/...` is Windows, any other `/`-rooted answer POSIX, anything
else raises); bare `PurePath` is
rejected because it would infer the local OS. SSH implies neither an OS nor a
shell. `shell="auto"` performs positive cached probing and raises rather than
guessing. The SSH executor provider closes its AsyncSSH connection and waits for closure.
AsyncSSH authentication failures are exposed as `PermissionError`; SSH
host-key, key-exchange, disconnect, connection-loss, protocol, and channel
failures are exposed as `ConnectionError`. The original AsyncSSH exception is
retained as `__cause__`.
The SFTP path provider reuses one `sftppath.AsyncSSHBackend` per host and
closes its connections during `close()`; each path call does not create
another SFTP connection. The path leg is given the exec leg's connect
options. Without `ssh_config` neither leg reads one: both receive
`config=None`, so an alias, `IdentityFile`, `UserKnownHostsFile` or
`ProxyCommand` in `~/.ssh/config` is ignored; pass the host, key and
known_hosts explicitly.
Both legs therefore reach the same server and verify its key the same way.
`SFTPPathProvider.probe()` reports `unavailable` (naming `hostctl[ssh]`) when
sftppath is missing, so selection routes around it; `shell="auto"` accepts
sh, dash, ash, ksh, ksh93, mksh, lksh, oksh, yash, posh, bash, zsh and fish
(csh and tcsh are refused); `ssh://h:0` is a `ValueError`, not port 22.
`SSHConfig(connect_timeout=30.0)` bounds establishing the connection in
seconds (`None` leaves asyncssh's own default; otherwise it must be > 0) and is
the URI key `connect_timeout`, omitted from `connection_uri` at 30. `connect()`
and `with host:` raise `OperationNotStartedError("no provider connected: ...")`
when a selector offered providers and none connected (one that connects is
enough); a refused exec request is a plain `OSError`, not a `ConnectionError`;
`close()` in a forked child forgets the parent's connection without closing it.
A relative SSH path (and `host.path()`) resolves against the login directory,
read once per connection generation with SFTP `realpath(".")` on the exec
connection, so building one connects; `str()` of the result is the resolved
path. An absolute spelling asks nothing.
PowerShell over SSH: `PowerShellFlavour.command(..., login_shell=, stdin_free=)`
renders `-Command SCRIPT` for a `cmd` login shell (one-line script, escaped for
the extra parse) and a POSIX one (always); a script with a `#` goes on the
channel's stdin (`-Command -`, `ShellCommand.stdin`) when `stdin_free`, which
`SystemHost` sets only for a login-shell provider whose call has no `input=`/
`stdin=`; a multi-line script or taken stdin falls back to `-EncodedCommand`
(CLIXML stderr, decoded by `SSHExecutor` for `run()`; endpoint protection flags
it). Every form starts with `$ProgressPreference='SilentlyContinue'`. `PWSH`
(major version 7+) adds `if ($PSStyle) { $PSStyle.OutputRendering =
'PlainText' }` there and at the start of `invocation()`'s script, so captured
output carries no ANSI colour when `TERM` names a colour terminal.
A timed-out `SSHProcess.wait()` keeps the output it drained for `read()`;
the exception carries a copy.
`host` and `username` are refused where OpenSSH would refuse them (a leading
`-`, whitespace, shell metacharacters; `${` in a username); the host must be
an address or a valid name (netimps decides that). `str()` of an SSH host's path is the path on
the server. Provider close performs all AsyncSSH operations through the
shared bridge. Omitted `run()` stdin is an explicit EOF stream, `bufsize=0` is
rejected, and a missing remote exit status is reported as return code `-1`.
Timeouts raise `subprocess.TimeoutExpired`: the executor sends TERM, waits up
to 2 s for the channel to close, then closes it and waits 1 s more, so the call
returns within about three seconds of the deadline. `orphaned` is false only
when the channel closed during the TERM grace.
A composed SSH host has two connections, each opened when first needed and
each authenticating once: the exec connection on the first command (or the
first relative path), hostctl's own; the SFTP connection on the first path
operation, `sftppath`'s backend, which takes connect options rather than a live
connection. A host used only for absolute paths never opens the exec one.
`_SSHTransport` renders in one place (`_render`) for `spawn()` and `info()`;
`SystemHost` renders `run()` itself. `bufsize` is
asyncssh's stdin CHUNK size here, not `subprocess`'s buffering policy, so
values below 512 are refused rather than obeyed. `run()` buffers the whole
response in memory even when `stdout=` names a sink, and a file-object
`stdin=` is read fully before dispatch; both are memory characteristics of
the buffered contract, not of the channel.
An SSH host is **not fork-safe** in the sense of inheriting a usable
connection: a child notices the loop it belongs to has changed and drops the
inherited connection without closing it (closing would tear down a socket the
parent still uses), then reconnects on its own. `shell="auto"`
retains the executable path reported by the successful probe. Persistent SSH
process reads, writes, EOF, and close operations use the same transport-error
normalization as `wait()`.

## Containers

`ContainerConfig(container, engine_url=None, username=None, workdir=None,
executable=None, shell="auto", pathname="auto", client_factory=None)` uses the optional
`container` extra and Docker Engine API. Inspection selects Linux/POSIX or
Windows/PowerShell semantics. `ContainerHost` supports buffered exec,
persistent sessions/TTYs, and archive-backed POSIX or Windows paths.
`pathname` is `"auto"`, `"posix"`, `"windows"` or a pure-path class. Archive
paths support stat/traversal/read/write/append/exclusive-create, plus
`symlink_to()`/`readlink()` -- a `SYMTYPE` tar member is a faithful
representation; archive-only mkdir/remove/rename/chmod raise
`NotImplementedError`. `run()` takes no stdin and no `timeout=` (Docker's
buffered exec streams neither, and the exec API has no cancellable deadline).
`iterdir()`/`walk()` pull one archive per directory, so a deep tree is
re-downloaded level by level -- list a large one through `run()` instead.
An overwrite keeps the existing file's owner and mode (the archive API takes
ownership from the tar header alone); a new file is uid/gid 0, mode 0644, and
`username=` applies to exec only. `exists()` and `open("x")` read one member header,
not the archive. A path taken before `ContainerHost.close()` refuses
(`ValueError`) instead of reopening the client.

## QEMU Guest Agent

`QEMUConfig(domain, transport="libvirt", connection=None, socket_path=None,
ssh=None, agent_timeout=10.0, max_reply_size=48*1024*1024, shell="auto",
pathname="auto", transport_factory=None, serial_console=None,
path_helper=None)` creates a
`QEMUHost`. Transports are local libvirt (`qemu-libvirt` extra), direct Unix
socket, or an AsyncSSH-tunneled remote Unix socket. Discovery positively probes
QGA and its enabled command list.

`QEMUExecutor` uses buffered `guest-exec`/`guest-exec-status`. It declares
argv only: `guest-exec`'s `env` list is applied as the guest's whole
environment rather than added to it, so the host embeds assignments in the
rendered script and a direct `Exec` refuses `cwd=`/`env=`. QGA cannot cancel
timed-out processes; `TimeoutExpired.orphaned` is true and `.pid` is retained
when known. `QGAPathBackend` uses bounded file-handle RPCs; `exists()` is
answered from those RPCs alone, while metadata and namespace mutations need a
guest helper (`QEMUConfig(path_helper=...)`) and raise `NotImplementedError`
without one. `write_bytes()` and `open("w")` write in place, not atomically, so
the file keeps its inode, mode, owner and links; only the exclusive `x` modes
need the helper (a temporary file and a rename that does not replace).
`rename()` follows `os.rename` for the guest family: it replaces on a POSIX
guest and raises `FileExistsError` on a Windows one. A listing skips any name
that is not one path component. `run(stderr=STDOUT)` interleaves the streams on
qemu-ga 8.0+ with a non-Windows guest and otherwise returns stdout followed by
stderr.

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

QGA paths get their metadata and namespace operations from a guest-side
helper, because the file RPCs move bytes and nothing else. One is PROBED during
discovery: `PosixGuestPathHelper` for a POSIX guest (GNU `stat -c` and
`find -printf`, proven by `stat -c %f -- /` answering hex for a directory) and
`WindowsGuestPathHelper` for a Windows one (the same PowerShell scripts
`WinRMPathBackend` uses, sent over `guest-exec` as `powershell.exe -Command -`
with the script as `input-data`, so no script is on the command line). A
guest that fails the probe keeps the degraded mode it had before -- reads and
writes never needed a helper -- rather than losing `path()`.
`QEMUConfig(path_helper=...)` overrides the probe entirely. Every path is an
argv element after a `--`, so a leading `-` is data and nothing is interpolated
into shell text; `find` takes no `--`, so it gets an absolute path or one
anchored with `./`.

`QEMUConfig.max_reply_size` (default 48 MiB) bounds one QGA reply, and
`agent_timeout` bounds one round trip independently of a command's `timeout=`:
an agent that stops answering raises `ConnectionError`, not a command timeout.
`run()` results carry `stdout_truncated`/`stderr_truncated` and warn when set.
`shell="auto"`/`pathname="auto"` require positive evidence of the guest
family (`guest-get-osinfo`, or a family-exclusive command in `guest-info`) and
raise rather than defaulting to POSIX. `info().os_family` is a family from
`kernel-name`, not the os-release `id`. A `QGAPathBackend` is invalidated by
`QEMUHost.close()`, so a path handed out earlier refuses instead of
reconnecting. `open("rb")` is seekable where `guest-file-seek` is advertised;
the exclusive `x` modes are refused by `open()` without a guest helper.

`QGACommandError` is a guest agent error reply, carrying `.error_class` and
`.description`; a path operation translates it into the matching `OSError`
(`FileNotFoundError`, `PermissionError`, ...). `QGAProtocolError` is a
malformed or oversized reply and is itself a `ConnectionError` (see
"Exceptions").
An injected `QEMUSerialConsole` adds the `serial` capability and
`QEMUHost.open_serial(*, encoding=None, errors=None)`. It is raw, exclusive,
and makes no shell/status claim; `encoding=` makes the console read and write
`str`.

## WinRM

`WinRMConfig(host, username, password=None, auth="ntlm", port=None,
tls=False, verify=True, message_encryption="auto",
operation_timeout=20.0, read_timeout=30.0, provider="auto",
allow_cleartext=False)`. `auth` is the authentication scheme (`ntlm`,
`kerberos`, `credssp`, `basic`, `plaintext`, `ssl`); `tls=True` is HTTPS
(`winrms://`); `verify=False` skips the server certificate check
(`?verify=false`); the timeouts are float seconds, rounded up where a leg
needs whole seconds. `provider` is `auto|native|pywinrm|psrp`. `auth` `basic`, `plaintext` or `ssl` over plain
HTTP sends the password in cleartext and raises `ValueError` unless
`allow_cleartext=True` (URI query `allow_cleartext=true`). `str(config)` omits
an unset port. `auto` picks one leg: a password-free config on a Windows client
is `native`; otherwise PSRP when `hostctl[psrp]` is installed on Python
3.10+, otherwise pywinrm. An explicit value forces a leg: `native` takes no
password, and `pywinrm`/`psrp` need one (or `auth="kerberos"`).
`provider="psrp"` requires the extra. PSRP `run()` text is rendered
by `Out-String -Stream` in the runspace, so tables look as on the other legs,
joined with CRLF at the runspace's 120 columns; `RunspaceSession.invoke()`
returns typed objects, and `invoke(script, *args)` renders its arguments with
the PowerShell flavour's `structured_command`.

The WinRM executor provider supports PowerShell `run()` and Windows-semantic paths (a `CompositeWindowsPath` over the WinRM path backend).
The pywinrm leg runs `powershell.exe -NoProfile -NonInteractive -Command -`
without cmd.exe and sends the script on stdin, so scripts and `run()` commands
have no command-line cap; every leg shares one 256,000-byte script budget.
Password-free configs on Windows use current-context native PowerShell
remoting. On that native path
`verify=False` becomes `New-PSSessionOption -SkipCACheck
-SkipCNCheck` (both checks, matching pywinrm), and the remote exit code is
carried back explicitly, so a native command failing only by status is
reported as a failure rather than as success. A WinRM path's `open("rb")` fetches
bounded ranges; writable modes stage content and transfer Base64 chunks on
close. WinRM stdin is
unsupported. The native leg's `read_timeout` is a total per-call deadline
(default 30 s) that `run(timeout=)` overrides; the remote command keeps running
(`orphaned=True`). pywinrm and PSRP have no total deadline and refuse
`timeout=`. Every leg reports one status: 0 when the last statement succeeded,
else the native exit code, else 1; a terminating error that sends no status is
1. Remote error records are a non-zero status with the text on stderr; only
transport and authentication failures raise (`ConnectionError`,
`PermissionError`). A remote ACL denial is a status, not `PermissionError`.
pywinrm Session has no guaranteed close API; hostctl calls `close()` only when
a provided session exposes it.
`has_psrp()` reports whether the optional PSRP dependency is installed;
`provider="psrp"` without it raises the install hint. PSRP runspaces
are exposed separately through the WinRM transport provider and
`RunspaceSession.invoke()`. They retain typed PowerShell streams and state, and
are not advertised as a byte-oriented `spawn`/TTY process.

## Local and utilities

`LocalHost.path()` and `LocalHost.run()` work on POSIX and Windows.
`LocalExecutor` provides native argv, cwd, environment, stream, encoding,
check, and timeout behavior through `subprocess.run`.

`SerialConfig(device, baudrate=115200, bytesize=8, parity="N", stopbits=1,
xonxoff=False, rtscts=False, dsrdtr=False, read_timeout=0.1, write_timeout=10,
inter_byte_timeout=None, exclusive=None, profile=RawConsoleProfile(),
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
Closing the host or `executor.close()` ends a live serial process: its reads and
writes raise `ConnectionError` and its own `close()` is a no-op, so a stale
handle can never free the lease of the process that replaced it.
`PromptConsoleProfile(..., login_timeout=10.0)` bounds each login step and the
first prompt (`ValueError` unless positive); it is not part of the URI.
`ConsoleProfile` is the profile contract, `LoginStep` one expect/send
pair of a login sequence, and `ConsoleProtocolError` what a profile raises
when the console does not answer the way the protocol requires.

WinRM, container and QGA paths share one base. `rename()` onto a path of
another host raises `OSError(EXDEV)`, which `move()` takes as the cue to copy
and delete; `chmod()` takes a mode int or an octal string. A write stream
(`open("w")`, `"r+"`, `"a"`) uploads once on `close()` and is closed even when
the upload fails, so write again to retry. `pathlib.Path(src).copy(dest)` onto
such a path raises `FileExistsError` for an existing directory and
`ValueError` for a source name that is not one path component.

`TerminalOptions(term, columns, rows, pixel_width, pixel_height)` is the
resolved terminal request `spawn(terminal=...)` produces.

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

- `HostctlError` -- the base; catch it for "anything hostctl raised".
- `TransportError` -- the connection to the target failed or was never made.
- `OperationNotStartedError(reason="operation was not started", *,
  cause=None)` -- a provider declined before anything was sent (`.reason`,
  `.cause`); see "Provider contract". Raised to the caller when every
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
- `QGACommandError(error_class, description, *, data=None)` -- the guest
  agent refused a command (`.error_class`, `.description`, `.data`); path
  operations translate it into the matching `OSError`.

## Command line

`hostctl.cli.main(argv=None) -> int` is the entry point of the `hostctl`
console script and of `python -m hostctl`, built on `duho`; install
`hostctl[cli]`. Without the extra it raises `SystemExit` naming it.
`hostctl.cli` is not library API and is absent from `hostctl.__all__`.

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
  root takes `--version`.
- Exit status: `run` returns the remote command's own status; the command
  line's own failures are 125 (connection, timeout, usage, unsupported
  operation), 126 (permission denied), 127 (not found) and 130 (interrupted).
  A parser usage error is 2. An unreachable host is 125 for every
  subcommand, `info` included (`SystemHost._collect_info()` says whether any
  provider answered; the library's `info()` stays best effort).
- `HOSTCTL_PASSWORD` supplies a password, offered only to schemes that accept
  one (decided per scheme: `qemu+libvirt:` and `qga+unix:` take none);
  `--ask-password` prompts instead and is refused in a tool call. A tool
  caller can read the variable and name any host to offer it to.
- `HOSTCTL_MCP=stdio` serves every subcommand as an MCP tool over stdio
  (`hostctl.run`, `.ls`, `.cat`, `.cp`, `.info`, `.shell`); `run`'s program
  is the tool argument `"--"`. A tool call has no stdin, so `shell` there
  sends nothing.
