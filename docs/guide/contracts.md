# Cross-transport contracts

Host implementations share a deliberately small, subprocess/pathlib-shaped
contract.  A provider may advertise only the capabilities it can implement;
callers should inspect `host.capabilities` before requesting optional
operations.

## Command execution

`Host.run(*cmds)` returns `subprocess.CompletedProcess` and captures stdout and
stderr by default.  Captured streams are bytes unless `text=True` or an
`encoding` is supplied; a silent stream is always `b""`, never `None`.

- A string is shell source and remains verbatim (operators such as `&&`, pipes,
  and redirects are not quoted).
- A tuple/list is argv data and each item is quoted by the selected shell.
- A leading `pathlib.PurePath` or `pathlib_next.Path` selects direct argv
  execution; following values are arguments and are never interpreted by a
  shell.
- Multiple top-level commands are joined with the shell's sequence separator.
- `env` is additive to the provider's environment. `cwd` is the process working
  directory. `check=True` raises `CalledProcessError` for every non-zero status;
  a provider which cannot obtain a status must use `-1`, never `None`.
- `timeout` raises `subprocess.TimeoutExpired` and the provider must make a
  best-effort attempt to terminate and close the child.

## Persistent processes

`Host.spawn()` returns a synchronous `Process`. `read(n)` returns up to `n`
available bytes (`n < 0` reads until EOF), `read_stderr` mirrors it, and EOF is
represented by `b""`. `wait()` is idempotent and returns an integer status;
`close()` is idempotent.  Implementations with a text encoding decode only at
   complete character boundaries. `send_eof`, `resize`, and terminal allocation
are optional capabilities and raise `NotImplementedError` when unavailable.

## Paths

`Host.path()` returns a `pathlib_next.Path`. Missing paths raise
`FileNotFoundError`; reading a directory raises `IsADirectoryError`; scanning a
file raises `NotADirectoryError`. `exists()` is a boolean probe and never raises
for a dangling symlink/reparse point. Binary and text writes must round-trip,
including empty files and multibyte text split across transfer chunks.

### Symbolic links

`symlink_to(target, target_is_directory=False)` and `readlink()` follow the
`pathlib.Path` signatures. `readlink()` reports the target the transport
stored, verbatim: a relative target stays relative and is never resolved
against the link's parent. `stat()` follows links by default and
`stat(follow_symlinks=False)` does not, so `is_symlink()` and `exists()`
always agree.

Local, SFTP (SSH), WinRM, and container archive paths implement both. QGA
paths raise `NotImplementedError` because the guest agent exposes no symlink
RPC. Creating a symbolic link on a Windows target additionally requires an
elevated session or Developer Mode; without one the backend raises
`PermissionError`, which is a host policy rather than a transport gap.

`target_is_directory` is a local-Windows-filesystem hint with no wire
representation, so every remote backend accepts and ignores it.

## Capability selection

The conformance tests are capability-driven. A skipped test must state the
missing capability (`pytest -rs`); providers must not silently skip behavior
they advertise.

## Divergence ledger

| Provider/operation | Deliberate divergence | Rationale |
| --- | --- | --- |
| Serial transport | raw profiles expose sessions only; prompt profiles opt into `run` only with explicit status framing; no filesystem | A serial byte stream has no portable command protocol. Profiles own login, prompts, line endings, and completion markers. |
| Serial process `read(-1)` | returns bytes currently reported as available, capped at 64 KiB, rather than waiting for EOF | Physical and network serial ports normally have no EOF until disconnected; waiting for EOF would make interactive sessions unusable. |
| WinRM persistent process | `spawn`/TTY unavailable | WinRM's buffered command API does not expose a durable bidirectional stream. |
| QEMU Guest Agent | timed-out guest processes cannot be cancelled | QGA exposes process IDs and polling but no portable kill operation in the buffered contract. |
| Container path mutations | unsupported operations raise `NotImplementedError` | Docker archive APIs provide safe file transfer but not all remote filesystem metadata primitives. Symlinks are the exception: a `SYMTYPE` tar member is a faithful archive representation, so `symlink_to()`/`readlink()` are implemented. |
| QGA `symlink_to()`/`readlink()` | raise `NotImplementedError` | The guest agent's `guest-file-*` protocol has no symlink RPC; emulating one through `guest-exec` would be a different transport with different permission semantics. |
| WinRM `symlink_to()` | may raise `PermissionError` | Windows requires an elevated session or Developer Mode to create a symbolic link. This is a host policy, not a transport gap, so the privilege failure is normalized rather than reported as an unsupported operation. |
| LocalPath on Windows | opening a directory may surface `PermissionError` | CPython's Windows file API reports EACCES; remote providers normalize this to `IsADirectoryError`. |
