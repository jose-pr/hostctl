# Cross-transport contracts

Host implementations share a deliberately small, subprocess/pathlib-shaped
contract.  A provider may advertise only the capabilities it can implement;
callers should inspect `host.capabilities` before requesting optional
operations.

## Command execution

`Host.run(*cmds)` returns `subprocess.CompletedProcess` and captures stdout and
stderr by default.  Captured streams are bytes unless `text=True`, an
`encoding`, or an `errors=` is supplied -- `errors=` alone selects text mode
on every transport, which is the rule 0.2.6 made uniform. A CAPTURED stream that produced no output is `b""`,
never `None`; a stream excluded by `capture_output=False` (or routed to a
target) is `None`, exactly as `subprocess` reports one it did not capture.

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

`Host.path()` returns a `pathapi.Path`. Missing paths raise
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
| pywinrm, PSRP and container `timeout=` | raises `NotImplementedError` | Neither WinRS, PSRP nor Docker exec exposes a cancellable command deadline, so a `timeout` there could only be a read deadline that leaves the remote command running -- which is not what the keyword means anywhere else. |
| Native WinRM deadline | `read_timeout_sec` bounds the whole call (default 30 s) and `run(timeout=)` overrides it per call; the remote command keeps running (`orphaned=True`) | The local `powershell.exe` relays `Invoke-Command`, which has no read deadline of its own, and an unbounded relay blocked forever on a hung host. pywinrm and PSRP poll for output instead, so they have no total deadline to offer. |
| PSRP `run()` text | stdout is rendered by `Out-String -Stream` in the runspace at a hostless runspace's 120 columns, where pywinrm and native use the far end's console width; stderr is each error record's message, not the console's error view | A runspace has no console, so the width is the runspace's own, and error records arrive on a separate typed stream that carries no rendered view. |
| Serial framed `run()` | output past `max_buffer` raises instead of truncating | A console transcript cut from the front mid-line and returned as complete is a corrupt result reported as success, and `error_patterns` in the discarded part stopped setting a status. The cap is a profile setting; raising it is the caller's decision to make, not the library's to make silently. |
| Serial process `read(-1)` | returns bytes currently reported as available, capped at 64 KiB, rather than waiting for EOF; a text-mode read returns `""` while a character is incomplete and keeps it for the next read | Physical and network serial ports normally have no EOF until disconnected; waiting for EOF would make interactive sessions unusable. |
| QGA `run()` captured output | may be truncated; the result carries `stdout_truncated`/`stderr_truncated` and a `RuntimeWarning` | qemu-ga caps what it captures and reports the cap in its status reply. There is no second chance to fetch the rest, so the result says it is incomplete instead of looking whole. |
| QGA `info().os_family` | a family (`linux`, `windows`, `freebsd`), never the distribution | `guest-get-osinfo` answers `id` with an os-release id (`ubuntu`, `rhel`); the family comes from `kernel-name`, so the same machine reads the same over QGA as over SSH. `id` and `pretty-name` feed `os_name`. |
| Serial `run()` with several commands | one framed exchange each, output concatenated, the LAST status reported | Joined with the profile's separator they went out as one line, which most device consoles parse as a single malformed command -- and a profile that frames status per exchange could only report one status for all of them. |
| Serial `run()` with a structured command | raises `NotImplementedError` | A serial host names no shell flavour, so there is no quoting rule to apply; `shlex.join` put POSIX quotes on a device that takes them literally. The profile owns the device's grammar -- pass the command as text. |
| Serial `spawn()` reads | `bytes`, unless `encoding=` or `errors=` is passed | A console is a byte stream and the profile's encoding is a write-side default; promoting it to a read mode would turn every existing session into a text one. With either keyword the reads decode, as they do on the SSH and QEMU console adapters. |
| SSH to a Windows target with the `cmd` dialect | the rendered command line is caret-escaped a second time | AsyncSSH sends one command STRING and the server hands it to a login shell. Windows OpenSSH's stock server uses `cmd.exe /c`, so a cmd command line -- which already carries one cmd layer -- is parsed by cmd twice. Measured against a real cmd.exe: unescaped, 4 of 8 adversarial values survive and two of the failures are command injection. Set `SshConfig(login_shell=...)` when the server's `DefaultShell` is not the stock one. |
| Local `path()` with no segments | returns a path with no segments added (`.`), not the absolute working directory | It was the one host that added a segment, so `host.path()` meant two different things depending on which host you had. The relative path still resolves against this process's working directory, so using it is unchanged -- only its text. A local path is also a plain `pathapi` path rather than a composite: one provider reaching one filesystem has nothing for a composite to decide. |
| QEMU console `read()` | returns one chunk (64 KiB for `read(-1)`), and an empty receive is treated as stream EOF: it ends the console lease and every later read returns the empty value | A libvirt console stream has no EOF of its own until the peer goes away, so waiting for one would make an interactive console unusable. An empty receive from a blocking console stream means the peer closed; the lease is released so the console can be opened again, and reads stay readable rather than raising. |
| WinRM persistent process | `spawn`/TTY unavailable | WinRM's buffered command API does not expose a durable bidirectional stream. |
| `run(check=)` and `run(capture_output=)` | both default to `True`, where `subprocess.run` defaults them to `False` | A remote command that fails is an error by default rather than a return value a caller may forget to inspect, and its output is captured rather than inherited by a process that may have no console. Pass `check=False` to read `returncode` yourself. |
| Batch files (`.bat`/`.cmd`): local `Exec`, `cmd` and both PowerShell flavours | each argument is one batch token: wrapped in `"`, `"` written `""`, `%` written `%%cd:~,%`, trailing backslashes doubled; a `"` or `%` in a local `Exec` program path raises | Windows dispatches a batch target through cmd.exe, which parses the argument again (CVE-2024-24576 class), so the token must be inert to cmd and survive the batch file's own `%*`. `""` is the only quote cmd keeps balanced. A Microsoft C runtime reads `""` as one `"`; a consumer using `CommandLineToArgvW` or the system `msvcrt.dll` (node, `robocopy` and other system tools) may split a value that has whitespace after a quote. It never executes. The program path is read by `CreateProcess` before cmd does, so no escape serves both. |
| Program of unknown type (a bare name, `.ps1`, other PATHEXT types) | `cmd` raises `ValueError` for an argument containing `"`; PowerShell raises for one containing any of `" & \| < >` | A bare name may resolve to a batch file, and a value PowerShell passes unquoted (no space) would then be executed by cmd. Name the program with its `.exe` or `.bat` suffix, use `Cmdlet` for a PowerShell cmdlet, or use `Exec`. `%`, `^` and `!` are not refused: a batch file corrupts them (`%USERNAME%` is expanded) but they cannot start a command. |
| Windows PowerShell 5 native arguments | raises `ValueError` for a value whose every whitespace follows an odd number of `"` | PS 5 wraps a native argument in quotes only when some whitespace sits at an even count of `"` (counting the `\"` hostctl emits), so such a value reaches the child split. `""` inside quotes would work for the Microsoft C runtime but not for `CommandLineToArgvW` or `msvcrt.dll` consumers. `PWSH` and `Exec` pass the value unchanged. |
| PowerShell native argument `--%` | raises `ValueError` | PowerShell 5.1 and 7 read a native argument whose value is `--%` as the stop-parsing token however it is spelled (`'--%'`, `"--%"`, `$('--%')` all do), so the rest of the line is passed unparsed. No spelling sends the value; a cmdlet argument is not affected (`Cmdlet`). |
| Windows PowerShell 5 `PIPE`/`REDIRECT`/`APPEND` between native programs | bytes are re-encoded: a pipe goes through the console code page, `>`/`>>` write UTF-16LE with a byte-order mark | A property of the engine: measured, `41e980ff0a` through a pipe arrived as `413f3f3f0d0a`. PowerShell 7 and cmd are byte-exact. A pipe between cmdlets is PowerShell's normal use and is correct, so the operator is not refused. |
| Embedded `env` value `""` under Windows PowerShell 5 and cmd | the variable is unset | `$env:K=''` and `set K=` delete the variable on those engines and neither has a spelling for an empty one; pwsh and POSIX set it empty, and a provider that carries `env` natively (local) keeps it empty. Refusing `""` would make a value that works natively fail when embedded. |
| Local `timeout=` from a terminal | a timed-out command started from a process that has a controlling terminal may leave grandchildren running | A new session (needed so one signal reaches the whole tree) drops the terminal, so a child opening `/dev/tty` failed. A new process group would keep it but run the child in the background, where reading the terminal stops it. Without a terminal the child still gets its own session and the whole tree is killed. |
| `cmd` structured `call` | raises `ValueError` | `call` re-expands `%` and doubles `^` in its arguments (measured: `%USERNAME%` became the user name, `c^d` became `c^^d`), so no single quoting survives it. Run the program directly; a `.bat`/`.cmd` already gets the batch rule without `call`. |
| `cmd` structured `start` | always renders an empty title, `start "" ...`; the tail is program and arguments | `start` reads its first quoted word as the window title, so a quoted program path ran as a title and nothing started. A structured `start` therefore never takes a title of its own; raw text still can. |
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
| QGA `write_bytes()`/`open("w")` | written in place, not atomically; `exclusive=True` writes a temporary file and renames it without replacing | In place is what keeps the file's inode, mode, owner and links, as a local `open("w")` does. QGA has no atomic create-if-absent RPC, so exclusive creation needs a temporary and a no-replace rename. A new file gets the guest agent's creation mode. |
| WinRM, container and QGA paths | no `Utime`, so `PathSyncer` cannot carry modification times there | None of the three transports exposes a set-mtime operation through hostctl's path backend. |
| QGA `rename()` | replaces an existing target on a POSIX guest, raises `FileExistsError` on a Windows guest | The same rule `os.rename` follows on each family. |
| Container `write_bytes()` | an overwrite keeps the owner and mode from the existing tar header; a new file is uid/gid 0, mode 0644, and `ContainerConfig(user=)` does not apply | The archive API takes ownership only from the tar header, and the configured user is an exec setting the engine never sees on `put_archive`. |
| QGA `run(stderr=STDOUT)` | interleaved only on qemu-ga 8.0+ with a non-Windows guest; otherwise stdout followed by stderr | Older agents capture the two streams separately, and `merged` is compiled out of the Windows agent. |
