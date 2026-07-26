# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

- Composite host paths kept their provider, selector, and pin through
  `pathlib.PurePath` derivations (`parents`, `with_name`, `with_suffix`,
  `relative_to`). Those results were previously built without any routing
  state, which made `glob()`, `rglob()`, and `walk()` fail outright.
- A path provider that declined before dispatch is remembered for the
  connection generation instead of being re-attempted by every later
  operation; `invalidate()` clears the record along with cached probes.

<!-- Add the [Unreleased] compare link after the first v0.1.0 tag exists. -->
