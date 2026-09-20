# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- The `pathlib_next` floor moves to `>=0.9.10` in both `dependencies` and
  the `ssh` extra. **0.9.4 through 0.9.9 cannot be used**: those releases
  decide "same file" and "overlapping trees" by falling back to path
  equality, which compares `(type, segments)` and knows nothing about
  hosts, so copying `/etc/app.conf` from one host to the same path on
  another was refused with `OSError [Errno 22] Source and target are the
  same file` (and `PathSyncer` with "source and target overlap"). 0.9.10
  adds the `_same_filesystem()` hook this release answers.

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

[Unreleased]: https://github.com/jose-pr/hostctl/compare/v0.2.7...HEAD
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
