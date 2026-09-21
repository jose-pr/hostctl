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
  best-effort attempt to terminate and close the child. The exception carries
  the same payload on every transport: `.orphaned` (`True` when the command
  could not be stopped and is still running), `.pid` (`None` unless the
  transport knows one), and `.output`/`.stderr` as empty rather than `None`.

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
| Local uncaptured output | inherits the parent's file descriptor rather than writing to `sys.stdout` | That is `subprocess.run`'s own `stdout=None` behaviour and it costs nothing to keep: the child writes where the parent writes. A buffered transport has the bytes in hand instead, so it must route them, and `dispatch_output` sends them to `sys.stdout`. |
| WinRM, PSRP and container `timeout=` | raises `NotImplementedError` | Neither WinRS, PSRP nor Docker exec exposes a cancellable command deadline, so a `timeout` there could only be a read deadline that leaves the remote command running -- which is not what the keyword means anywhere else. |
| Serial framed `run()` | output past `max_buffer` raises instead of truncating | A console transcript cut from the front mid-line and returned as complete is a corrupt result reported as success, and `error_patterns` in the discarded part stopped setting a status. The cap is a profile setting; raising it is the caller's decision to make, not the library's to make silently. |
| Serial process `read(-1)` | returns bytes currently reported as available, capped at 64 KiB, rather than waiting for EOF | Physical and network serial ports normally have no EOF until disconnected; waiting for EOF would make interactive sessions unusable. |
| QGA `run()` captured output | may be truncated; the result carries `stdout_truncated`/`stderr_truncated` and a `RuntimeWarning` | qemu-ga caps what it captures and reports the cap in its status reply. There is no second chance to fetch the rest, so the result says it is incomplete instead of looking whole. |
| QGA `info().os_family` | a family (`linux`, `windows`, `freebsd`), never the distribution | `guest-get-osinfo` answers `id` with an os-release id (`ubuntu`, `rhel`); the family comes from `kernel-name`, so the same machine reads the same over QGA as over SSH. `id` and `pretty-name` feed `os_name`. |
| PowerShell structured `-Name` tokens | rendered unquoted | PowerShell's binder does not read a quoted string as a parameter name, so `["Remove-Item", "-LiteralPath", path]` bound positionally and deleted nothing. The pattern (`-` plus letters and digits, optional trailing `:`) admits nothing that needs quoting, and a native program receives the same token either way. |
| QEMU console `read()` | returns one chunk (64 KiB for `read(-1)`), and an empty receive is treated as stream EOF: it ends the console lease and every later read returns the empty value | A libvirt console stream has no EOF of its own until the peer goes away, so waiting for one would make an interactive console unusable. An empty receive from a blocking console stream means the peer closed; the lease is released so the console can be opened again, and reads stay readable rather than raising. |
| WinRM persistent process | `spawn`/TTY unavailable | WinRM's buffered command API does not expose a durable bidirectional stream. |
| `run(check=)` and `run(capture_output=)` | both default to `True`, where `subprocess.run` defaults them to `False` | A remote command that fails is an error by default rather than a return value a caller may forget to inspect, and its output is captured rather than inherited by a process that may have no console. Pass `check=False` to read `returncode` yourself. |
| Local `Exec` of a `.bat`/`.cmd` | the invocation is quoted for cmd.exe | Windows dispatches a batch target through cmd.exe, which re-parses the C-runtime-quoted line with cmd rules (CVE-2024-24576 class). The inserted shell is the platform's, so hostctl escapes for it rather than letting an argument close the quoting or `%VAR%` expand. A `"` inside a value still cannot round-trip through a batch `%1`. |
| `cmd` dialect over SSH | the rendered command is escaped for one cmd parse, not two | `ShellFlavour.command()` is read by the remote login shell, and Windows OpenSSH's default shell is cmd.exe -- which consumes that one layer before the inner `cmd /c` sees it. The PowerShell dialect has no such gap (it renders `-EncodedCommand`, inert under any parser), so a Windows target reached over SSH should use it. |
| `cmd` and `invocation()` | raises `NotImplementedError` | A cmd script cannot survive argv delivery: the platform's own command-line quoting escapes the quotes cmd's parser needs, and no argv element can encode an unescaped one. Render with `command()` and submit the result as an `executor.CommandLine`. |
| QGA direct `Exec` with `cwd=`/`env=` | raises `NotImplementedError` | `guest-exec` has no cwd at all, and its `env` list is passed to the guest as `envp`, which replaces the environment rather than adding to it. A direct argv execution has no shell to embed additive assignments into, so it refuses rather than running with a different meaning than every other transport. |
| QEMU Guest Agent | timed-out guest processes cannot be cancelled | QGA exposes process IDs and polling but no portable kill operation in the buffered contract. |
| Container `run()` | no `stdin`/`input=`, and `timeout=` raises `NotImplementedError` | Docker Engine's buffered `exec_run` does not stream stdin, and the exec API exposes no cancellable deadline -- a `timeout` there could only be a read deadline that leaves the command running, which is not what the keyword means anywhere else. |
| Container `iterdir()`/`walk()` | pulls one archive per directory, recursively | Docker's `get_archive` answers with the whole subtree, so a walk re-downloads the children at every level. It is correct and it is O(depth) in transfer; a large tree is cheaper to list through `run()`. |
| Container path mutations | unsupported operations raise `NotImplementedError` | Docker archive APIs provide safe file transfer but not all remote filesystem metadata primitives. Symlinks are the exception: a `SYMTYPE` tar member is a faithful archive representation, so `symlink_to()`/`readlink()` are implemented. |
| QGA `symlink_to()`/`readlink()` | raise `NotImplementedError` | The guest agent's `guest-file-*` protocol has no symlink RPC; emulating one through `guest-exec` would be a different transport with different permission semantics. |
| WinRM `symlink_to()` | may raise `PermissionError` | Windows requires an elevated session or Developer Mode to create a symbolic link. This is a host policy, not a transport gap, so the privilege failure is normalized rather than reported as an unsupported operation. |
| LocalPath on Windows | opening a directory may surface `PermissionError` | CPython's Windows file API reports EACCES; remote providers normalize this to `IsADirectoryError`. |
