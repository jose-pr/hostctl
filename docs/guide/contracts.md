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
- `Exec(program, *args)` is the only direct-execution spelling: one program
  with an argv, never interpreted by a shell, and never combined with other
  commands. A path anywhere else is an ordinary value that stringifies.
- Multiple top-level commands are joined with the shell's sequence separator.
- `env` is additive to the provider's environment, on every transport
  including the local one. `cwd` is the process working
  directory. **`check` defaults to `True`**, the opposite of
  `subprocess.run`: a non-zero status raises `CalledProcessError` unless the
  caller passes `check=False`. A provider which cannot obtain a status must
  use `-1`, never `None`.
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
| Serial framed `run()` | output past `max_buffer` raises instead of truncating | A console transcript cut from the front mid-line and returned as complete is a corrupt result reported as success, and `error_patterns` in the discarded part stopped setting a status. The cap is a profile setting; raising it is the caller's decision to make, not the library's to make silently. |
| Serial process `read(-1)` | returns bytes currently reported as available, capped at 64 KiB, rather than waiting for EOF | Physical and network serial ports normally have no EOF until disconnected; waiting for EOF would make interactive sessions unusable. |
| WinRM persistent process | `spawn`/TTY unavailable | WinRM's buffered command API does not expose a durable bidirectional stream. |
| `run(check=)` and `run(capture_output=)` | both default to `True`, where `subprocess.run` defaults them to `False` | A remote command that fails is an error by default rather than a return value a caller may forget to inspect, and its output is captured rather than inherited by a process that may have no console. Pass `check=False` to read `returncode` yourself. |
| Local `Exec` of a `.bat`/`.cmd` | the invocation is quoted for cmd.exe | Windows dispatches a batch target through cmd.exe, which re-parses the C-runtime-quoted line with cmd rules (CVE-2024-24576 class). The inserted shell is the platform's, so hostctl escapes for it rather than letting an argument close the quoting or `%VAR%` expand. A `"` inside a value still cannot round-trip through a batch `%1`. |
| `cmd` dialect over SSH | the rendered command is escaped for one cmd parse, not two | `ShellFlavour.command()` is read by the remote login shell, and Windows OpenSSH's default shell is cmd.exe -- which consumes that one layer before the inner `cmd /c` sees it. The PowerShell dialect has no such gap (it renders `-EncodedCommand`, inert under any parser), so a Windows target reached over SSH should use it. |
| `cmd` and `invocation()` | raises `NotImplementedError` | A cmd script cannot survive argv delivery: the platform's own command-line quoting escapes the quotes cmd's parser needs, and no argv element can encode an unescaped one. Render with `command()` and submit the result as an `executor.CommandLine`. |
| QGA direct `Exec` with `cwd=`/`env=` | raises `NotImplementedError` | `guest-exec` has no cwd at all, and its `env` list is passed to the guest as `envp`, which replaces the environment rather than adding to it. A direct argv execution has no shell to embed additive assignments into, so it refuses rather than running with a different meaning than every other transport. |
| QEMU Guest Agent | timed-out guest processes cannot be cancelled | QGA exposes process IDs and polling but no portable kill operation in the buffered contract. |
| Container path mutations | unsupported operations raise `NotImplementedError` | Docker archive APIs provide safe file transfer but not all remote filesystem metadata primitives. Symlinks are the exception: a `SYMTYPE` tar member is a faithful archive representation, so `symlink_to()`/`readlink()` are implemented. |
| QGA `symlink_to()`/`readlink()` | raise `NotImplementedError` | The guest agent's `guest-file-*` protocol has no symlink RPC; emulating one through `guest-exec` would be a different transport with different permission semantics. |
| WinRM `symlink_to()` | may raise `PermissionError` | Windows requires an elevated session or Developer Mode to create a symbolic link. This is a host policy, not a transport gap, so the privilege failure is normalized rather than reported as an unsupported operation. |
| LocalPath on Windows | opening a directory may surface `PermissionError` | CPython's Windows file API reports EACCES; remote providers normalize this to `IsADirectoryError`. |
