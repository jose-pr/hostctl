# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `ConnectionString` parses a connection target from whatever a user typed.
  `ConnectionString("nas", default_scheme="wss")` and
  `ConnectionString("nas:8443", ...)` parse, because a bare host is not an
  invalid URI — it is a URI with the scheme left off, which is what people
  type on a command line. A `default_ports` mapping fills a missing port
  (`ws`/`wss` are the case in point: no system services database knows them);
  an explicit port always wins.

  Credentials are parsed but never rendered. `str()` and `repr()` both emit
  the redacted form, with the password **removed rather than masked**, so the
  output stays a valid, reusable connection string that cannot round-trip a
  wrong credential — and a value reaching a log line or a traceback frame
  cannot leak one. A password may carry `key:value` extras after a newline,
  as `parse_credentials` describes, written raw.

  The host keeps the spelling it was given, and `is_local` is a string check
  that resolves nothing — it is called while a configuration is built, which
  is network-free, and resolving would both block and raise on a name that
  does not resolve. `qsl` and `query_val()` read the query; `replace()`
  returns a changed copy.

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

[Unreleased]: https://github.com/jose-pr/hostctl/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/jose-pr/hostctl/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/jose-pr/hostctl/compare/v0.1.0...v0.1.2
[0.1.0]: https://github.com/jose-pr/hostctl/releases/tag/v0.1.0
