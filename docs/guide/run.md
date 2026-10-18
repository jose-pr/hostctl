# Running commands

Concrete hosts implement `Host.run(...)` where their transport can support it:
`LocalHost` uses POSIX sh or Windows PowerShell according to the local platform,
`SSHConfig(...).open()` uses `asyncssh`, and
`WinRMConfig(...).open()` uses PowerShell through `pywinrm` or
the current Windows security context. `ContainerHost` uses Docker Engine exec,
and `QEMUHost` uses QEMU Guest Agent `guest-exec`.

```python
result = host.run("echo hello")
print(result.stdout)
```

## Command syntax

`run(*cmds, **options)` takes **one positional argument per command** — it is
varargs, not a list of commands. This is the distinction to get right:

```python
host.run(["chmod", "755", target])                      # ONE command
host.run(["chmod", "755", target], ["chown", "u", target])  # TWO commands
```

A list is *one* command whose elements are quoted individually. Wrapping
commands in an outer list does not produce several commands — the inner lists
are stringified into a single nonsensical argument:

```python
host.run([["chmod", "755", target]])   # WRONG: one command, mangled
```

The mirror mistake is passing an argv as separate positionals, which makes each
element its own command:

```python
host.run("chmod", "755", target)       # WRONG: runs chmod;755;/path
```

### The three forms a command may take

| Form | Example | Result |
| --- | --- | --- |
| Structured `list`/`tuple` | `["chmod", "755", p]` | `chmod 755 '/tmp/a b'` — every element quoted as data |
| Raw `str` | `"echo $HOME"` | verbatim shell text; globs, pipes, and `$VAR` are interpreted |
| `Exec(...)` | `Exec("/bin/ls", "-l", p)` | direct execution: one program plus argv, no shell |

Prefer the structured form. It quotes each element, so a value containing a
space, quote, `$`, or `;` is passed as data instead of changing the command.
Use a raw string only when you want the shell to interpret the text.

Elements may be `str`, `bytes`, `int`, or path objects; all are normalized and
quoted. `["echo", 42, b"by", PurePosixPath("/p q")]` renders as
`echo 42 by '/p q'`.

### Joining and operators

Commands are joined with the flavour's separator (`;` on POSIX). Pass a
`ShellOperator` *between* two commands to join them conditionally instead:

```python
from hostctl import ShellOperator

host.run(["test", "-f", target], ShellOperator.AND, ["rm", target])
```

`ShellOperator` provides `AND`, `OR`, `PIPE`, `REDIRECT`, `APPEND`, and
`SEQUENCE`. An operator that is leading, trailing, or adjacent to another
raises `ValueError`, and a flavour may reject one it cannot represent
portably — PowerShell 5 rejects `AND`/`OR`, while PowerShell 7 accepts them.

After `REDIRECT` or `APPEND` the operand is a file name: raw text stays shell
source, and a path (or a one-element tuple) is quoted for the shell itself, so
`host.run("echo hi", ShellOperator.REDIRECT, path)` writes `path` even when it
holds spaces.

### `Exec` is direct execution

`Exec(program, *args)` runs one program with an argv, with no shell layer
rendered — nothing quotes, splits, or interprets the values:

```python
from hostctl import Exec

host.run(Exec("/bin/ls", "-l", target))   # absolute path
host.run(Exec("ls", "-l"))                # resolved through the target's PATH
```

The program and each argument may be a `str`, `bytes`, or a path object. All
of them reach the transport as text, so the spelling records how you happened
to hold the value rather than changing what runs. A bare name is only possible
here: a plain string command is always shell text.

Arguments are argv values, never nested commands — a list or tuple raises
`TypeError` rather than blurring the two.

Direct execution replaces the whole call, so an `Exec` cannot be combined with
other commands: there is no shell to join them with, and running just one
would silently drop the rest. Give each its own call, or use structured
commands if you want them joined.

Everything else is an ordinary value. A path is just a value that
stringifies, wherever it appears:

```python
host.run(["chmod", "755", Path("/srv/app")])   # a quoted argument
host.run(Path("/bin/a"), Path("/bin/b"))       # two shell commands
```

### Validation

An empty structured command raises `ValueError`, and so does a control
character in a *value* -- including a newline, which could otherwise smuggle
in a second command. Raw strings are shell source rather than values, so they
may span lines; NUL and the other non-whitespace control characters are still
refused. An empty raw string is skipped when joining.

### PowerShell targets

PowerShell flavours render a structured command through the `&` call operator
and append an exit epilogue so the remote status propagates:

```
& 'Get-Item' 'C:/a b'
$host.SetShouldExit($(if ($?) { 0 } elseif ($LASTEXITCODE) { $LASTEXITCODE } else { 1 }))
```

Both channels are consulted because `$LASTEXITCODE` is set only by *native*
commands: a failing cmdlet leaves it `$null` (so `exit $LASTEXITCODE` reported
success), and it goes stale, so a native failure followed by a successful
cmdlet reported the old code. The epilogue is on its own line so a script
ending in a `#` comment cannot swallow it, and it sets the exit code with
`$host.SetShouldExit` rather than `exit`, which drops the objects still
waiting to be formatted (`[pscustomobject]@{a=1}` printed nothing).

`run()` returns a `subprocess.CompletedProcess`. Highlights:

- `capture_output` may be `True` (both streams), `"stdout"`, `"stderr"`, or
  `False`.
- **`check` defaults to `True`**, unlike `subprocess.run`. A non-zero
  status raises `CalledProcessError` unless you pass `check=False`; a remote
  command that fails is an error by default rather than a return value to
  inspect.
- `timeout=` is seconds for this call and `None` no limit. Left out, it is
  the config's total (`SSHConfig(host, timeout=60)`), and with no total set,
  the transport's own default: no limit everywhere except WinRM (30 s). See
  [Timeouts](connecting.md#timeouts) for the other fields a config bounds.
- SSH `input=`, `cwd=`, `env=`, `timeout=` and `encoding=`/`errors=` follow
  the subprocess-shaped contract.
- WinRM buffers output to caller-owned file handles. It cannot stream stdin or
  select another executable. The pywinrm leg sends the script on stdin to a
  directly started `powershell.exe`, so a command has no command-line length
  limit. `run(timeout=)` is a total deadline on every leg, and the config's
  total (30 s unless `WinRMConfig(timeout=)` says otherwise; `None`: no
  default) is its default. When it
  expires the command is asked to stop (the native relay is killed, pywinrm
  sends the WinRS terminate signal, PSRP its stop signal) and `TimeoutExpired`
  carries `orphaned=True`, since none of them confirms the remote process
  ended. `timeout.connect` bounds only establishing the connection and
  never ends a running command. Every leg reports one status: 0 when the last
  statement succeeded, else the native exit code, else 1. A remote error is that
  status with its text on stderr; only transport and authentication failures are
  normalized to `ConnectionError`/`PermissionError`.
- SSH command dialect may be explicit (`POSIX_SHELL`, `POWERSHELL`, etc.) or
  positively detected with `SSHConfig(shell="auto")`. Structured commands,
  environment variables, and working directories use dialect-specific quoting.

## Persistent shell sessions

SSH and container hosts expose a persistent shell separately from buffered
`run()`:

```python
with host.shell.session(terminal=True, encoding="utf-8") as session:
    session.send(["printf", "%s", "quoted value"])
    session.send("echo raw | sed s/raw/stream/")
    output = session.read()
```

When no session options are needed, the shell itself is a context manager
opening one default session and closing it on exit:

```python
with host.shell as session:
    session.send("echo hi")
    output = session.read()
```

Both forms yield the same `ShellSession`. Use `session(...)` to pass a starting
command, `cwd`, `env`, `terminal`, or `encoding`. A shell can be entered again
after its session closes, but not while one is still open — that raises
`RuntimeError` rather than silently sharing or leaking a process.

## Shell defaults

Calling `host.shell(...)` returns a shell carrying defaults for every later
`run()` and `session()` that does not pass its own value:

```python
shell = host.shell(cwd="/srv/app", env={"TZ": "UTC", "LANG": "C"})

shell.run("pwd")                      # runs in /srv/app with TZ and LANG set
shell.run("pwd", cwd="/tmp")          # the call wins: /tmp
shell.run("printenv", env={"TZ": "EST"})   # TZ=EST, LANG=C still applied

with shell as session:                # the session inherits both
    session.send("pwd")
```

`cwd`, `encoding`, and `errors` are replaced by a per-call value. `env`
**merges per key**, so a call can change one variable without restating the
rest. `configure(...)` returns a further-configured copy and leaves the
original untouched, which matters because `host.shell` builds a new shell on
each access. Bare `host.shell` carries no defaults.

### Opting out of the shell environment

`env` defaults to an empty mapping — merge nothing, inherit the shell's
environment. Pass `None` to run without it:

```python
shell = host.shell(cwd="/srv/app", env={"TZ": "UTC"})

shell.run("printenv")             # TZ=UTC applied
shell.run("printenv", env={})     # same: merges nothing, inherits
shell.run("printenv", env=None)   # no shell-configured environment
```

`env=None` declines the shell's defaults — it does **not** request an empty
environment. Nothing is sent to override the target, so the command still runs
with whatever the host provides on its own: a login profile, rc files, the
service environment. Declining `env` also says nothing about `cwd`, which still
applies. `configure(env=None)` returns a copy carrying no environment default,
and `session(env=None)` opens a session without one.

Requesting a genuinely empty environment is a separate capability that does not
exist yet.

Defaults apply wherever the shell builds a script. A `Shell` over a host is
that host's `run`: `host.shell.run(x)` hands the provider exactly what
`host.run(x)` does. A `Shell(flavour, executor)` over a bare executor renders
the script itself and embeds `cwd`/`env` unless the executor carries them.
A `Shell` is not an `Executor`: calling it configures it, and running a program
with argv is `host.executor(command, *args)` or `host.run(Exec(...))`.

`session.send(*cmds, cwd=..., env=...)` accepts the same raw strings, structured
argument lists, multiple commands, paths, and `ShellOperator` values as `run()`.
Changing directory or environment inside the session persists in that shell.
`TerminalOptions` selects the terminal type and initial size; `resize()` changes
it later. TTY sessions combine stdout and stderr.

The streaming interface deliberately exposes `send()`, `read()`, and
`read_stderr()` rather than pretending that one read corresponds to one command.
Command-correlated capture requires a separate framing protocol.

WinRM's default provider is `auto`, and it picks one of three legs. A
password-free config on a Windows client uses `native` current-context
remoting. Otherwise it selects the optional PSRP provider when `hostctl[psrp]`
is installed on Python 3.10+, and falls back to `pywinrm`. Select
`provider="native"`, `"pywinrm"` or `"psrp"` to force a leg; `"pywinrm"` and
`"psrp"` need a password (or `auth="kerberos"`), and `"native"` takes
none. PSRP exposes a persistent typed runspace rather than a TTY:

```python
with WinRMConfig(
    "windows.example.com", username="admin", password="secret", provider="psrp"
) as host:
    with host.runspace() as session:
        session.invoke("$x = 1")
        result = session.invoke("$x")
        print(result.output, result.streams.error)
```

Install `hostctl[psrp]` for Python 3.10+; Python 3.9 remains on the portable
buffered `pywinrm` provider. PSRP `run()` returns the text a console would show
(tables included), joined with CRLF, rendered at the runspace's own width.

`auth="basic"`, `"plaintext"` or `"ssl"` over plain HTTP sends the password
in cleartext, so `WinRMConfig` refuses it unless you pass `tls=True` (a
`winrms://` URI) or `cleartext_auth=True`.

## Serial and other consoles

A serial host has two entry points, and they take different first arguments.
The constructor takes the **device**, the way pyserial names it --
`SerialConfig("COM3")`, `SerialConfig("/dev/ttyUSB0")`,
`SerialConfig("rfc2217://host:4001")` -- while the URI dispatcher takes the
**connection URI**, with the device inside it: `Host("serial:///COM3")`.
Passing a URI to the constructor asks pyserial to open a port literally named
`serial:///COM3`, which no system has. Its default
`RawConsoleProfile` exposes `host.shell.session()` with a merged byte stream,
serial break, DTR, and RTS controls, but no filesystem or command status.
Login steps and credentials are supplied programmatically and are never placed
in the URI. Two profiles add `host.run()`; the profiles live in
`hostctl.console` and drive any console stream, not only a serial port.

### A POSIX shell's console: `PosixConsoleProfile`

When the far end is a POSIX shell -- a Linux serial getty, a QEMU guest
console, a container TTY -- use `PosixConsoleProfile`. It never parses the
echo, so nothing the terminal does to what is typed can reach the output:

```python
from hostctl import PosixConsoleProfile, SerialConfig

config = SerialConfig(
    "/dev/ttyUSB0",
    profile=PosixConsoleProfile(login=[("login: ", "root"), ("Password: ", "secret")]),
)
with config as host:
    host.run("uname -a").stdout              # exactly what uname printed
    host.run("sh -c 'exit 3'", check=False).returncode  # 3
    host.run("wc -c", input=b"\x00\x03 binary").stdout  # stdin is a file
```

- The command and its `input=` are typed base64-encoded inside fixed shell
  syntax: a ^C, TAB, `!` or newline in them is data, never a keystroke the
  line editor acts on.
- Output, stderr and the exit status come back between markers the shell
  prints. Each marker is typed in two quoted halves, so the terminal's echo of
  the line never contains one; banners, prompts and line-editor redraws fall
  outside every frame. `stty -opost` makes the output the program's exact
  bytes, and the terminal's settings are restored afterwards.
- stdin is a temporary file holding `input=` (or a `stdin=` stream read to its
  end), or `/dev/null`: a command that reads stdin gets EOF instead of waiting
  on the terminal, and nothing the command leaves unread is ever run by the
  shell. stderr is separate unless `stderr=subprocess.STDOUT`.
- On `timeout` the profile types ^C, which interrupts the command and discards
  the rest of its line, and raises `TimeoutExpired` with the output so far.
- Every typed line stays under 1 KiB (a minimal busybox line editor drops a
  longer one), and at most `batch` bytes -- 2 KiB by default -- are typed
  before the shell acknowledges them, so a serial line's 4 KiB input queue
  never overflows.

Each acknowledgement is a round trip. On a stream that cannot overflow --
one that blocks the writer, with a far end that keeps reading the terminal,
like a web shell over a websocket or an SSH or container session --
`PosixConsoleProfile(batch=None)` types everything without waiting. Keep the
default on a serial line. A pty you read and write yourself from one thread
takes a larger batch (8 and 16 KiB measured on sh, bash, zsh and busybox) but
not `None`: while its echo goes unread it stops reading input, and the write
never returns.

The command runs in the console's own shell, so `cd` and variables persist and
`exit` ends the session.

The profile also names that shell's flavour, so the host speaks its grammar
as any other POSIX host does: `host.shell_flavour` is `POSIX_SHELL`, a
structured command is quoted by it, and `cwd=`/`env=` are embedded in the
script. They run in a subshell, so they apply to that one call and never to
the console's shell:

```python
with config as host:
    host.run(["printf", "%s", "a b"]).stdout       # b"a b"
    host.run(["ls", "-l", name], cwd="/var/log")   # quoted, in /var/log
    host.run("env", env={"LANG": "C"})             # only for this command
    host.run("pwd")                                # unchanged by the cwd above
```

Several commands, and `ShellOperator`s between them, form one script, run as
one exchange; `input=` is that script's stdin. For a zsh console pass
`PosixConsoleProfile(shell="zsh")`: zsh expands a bare `=word`, which the POSIX
flavour leaves unquoted. `host.shell` is an ordinary `Shell`, so
`host.shell(cwd="/srv")` binds defaults as it does on every other host.

A QEMU console or a container session is driven the same way, through
`console_stream`:

```python
from hostctl import PosixConsoleProfile, console_stream

profile = PosixConsoleProfile()
with guest.open_serial() as process:
    console = console_stream(process)
    profile.negotiate(console)
    stdout, stderr, status = profile.execute(console, "uptime", timeout=30)
```

### A device console: `PromptConsoleProfile`

A network device or a BMC has no POSIX shell to frame for it.
`PromptConsoleProfile(prompt=..., status_marker=..., reliable_status=True)`
opts into framed `host.run()` after defining the device's prompt and
completion marker. Its exchange owns a **window**: it starts after the command
the profile echoed back and ends at the first prompt following the completion
marker. That is what makes framing trustworthy on a stream with no
request/response correlation:

- Whatever the previous exchange left behind is discarded before the command
  is sent, so a timed-out `run()` cannot hand its tail to the next one.
- A prompt-shaped or marker-shaped string in the echo or in the output is
  output. The read ends at the marker the device appended last, then at the
  prompt after it.
- Paged output accumulates: each `--More--` is removed, the page is kept.
- Nothing is dropped silently. Output past `max_buffer` raises
  `ConsoleProtocolError` naming the cap rather than returning a truncated
  transcript with status 0; raise `max_buffer` on the profile to read more.
- `run(timeout=)` bounds the backend read itself, so a
  `SerialConfig(timeout=Timeout(read=None))` host raises `TimeoutExpired` instead of
  blocking inside the driver.

What the window cannot do is listed in the
[divergence ledger](contracts.md#divergence-ledger): it depends on the device
echoing the command as sent, carries no stdin, and merges the streams. Device
names are opaque (native ports, `loop://`, `socket://`, and RFC 2217 URLs are
passed to PySerial); RFC 2217 provides no encryption or authentication and
must be protected by an external secure transport.

## What parses a command on arrival

A transport that delivers a **command line** hands it to something that parses
it. `CreateProcess` does not parse; a login shell does. SSH is the case worth
naming: AsyncSSH sends one string, and the server runs it through a login
shell. On POSIX that is `$SHELL -c`, which is exactly the single parse this
package renders for. Windows OpenSSH's stock server uses `cmd.exe /c`, so a
`cmd`-dialect command line is parsed by cmd *twice* -- and unescaped, that is
command injection, not just corruption.

`run()` and `spawn()` share the rule. On a POSIX server the string is joined
so that every sh-family shell (sh, bash, zsh, dash, BusyBox ash) *and fish* read
it back verbatim; csh and tcsh are not supported. A fish login shell honours a
backslash inside single quotes where sh does not, so a backslash and a single
quote are written outside the quotes.

`SSHConfig(login_shell="auto" | "cmd" | "powershell" | "posix")` says which.
`auto` follows the path grammar: a Windows `pathname` means the stock `cmd.exe`.
The server's shell is configurable (`HKLM\SOFTWARE\OpenSSH\DefaultShell`),
so name it explicitly when it has been changed -- guessing is wrong in both
directions.

The PowerShell dialect picks how its script travels by what parses it. Under a
`cmd` login shell a one-line script goes as `-Command SCRIPT`, escaped for that
parse; under a POSIX login shell `-Command` always. A script that cannot cross
`cmd.exe /c` as an argument (it carries a `#` comment, which the status
epilogue must not follow on the same line) is sent on the channel's standard
input (`-Command -`) when the call passes no `input=`/`stdin=` of its own. Only
a multi-line script, or one whose stdin is taken, falls back to
`-EncodedCommand`: it is inert under any parser, but it writes CLIXML to
stderr, endpoint protection flags it, and `run()` decodes that stderr back to
text (a spawned process's stderr stays CLIXML).

## What a rendered command may contain

A **raw string** is shell source and stays verbatim, so it may span lines and
carry tabs -- a heredoc, an `if`/`for` block, an embedded `awk` program. Only
NUL and the other non-whitespace control characters are refused there: nothing
in any supported shell needs them, and they defeat terminal and log inspection.
A **structured element** is a value, and a value carrying any control character
at all is refused, because a newline inside a quoted argument is how a second
command gets smuggled in.

### Structured commands on Windows

A structured command's first element is the program, and what follows is
quoted for whatever parses it next, which on Windows depends on the program:

- **`.exe`/`.com`** is a native program. Windows PowerShell 5 raises
  `ValueError` for an argument it would split (every space after an odd number
  of quotes); use `PWSH` or `Exec` for those.
- **`.bat`/`.cmd`** is a batch file, whose arguments cmd parses again. Each
  argument becomes one batch token, so none of `& | < > ^ "` can run anything.
- **A cmd builtin** (`del`, `mkdir`, ...) is split by cmd itself.
- **Anything else** (a bare name, `.ps1`) may turn out to be a batch file, so
  an argument containing `"` (cmd) or `" & | < >` (PowerShell) raises
  `ValueError`. Name the program with its suffix.

PowerShell cannot tell a cmdlet from a program without a runspace. `Cmdlet`
is the spelling for a cmdlet, function or alias: `Cmdlet("Write-Output", text)`
renders PowerShell string literals, applies no C-runtime escaping and refuses
nothing. `PSParam("-LiteralPath")` renders a parameter name bare, which the
binder needs; a plain string is always a value::

    host.run(Cmdlet("Remove-Item", PSParam("-LiteralPath"), path))

A path given as a whole command is a one-element command, so PowerShell runs
it with the call operator instead of printing it. Under `cmd`, a structured
`start` always renders an empty window title (`start "" ...`) and a structured
`call` raises `ValueError`: run the program directly.

Both raise `TypeError` for any other shell. Where a value that looks like a
parameter name is meant as data, pass it as a plain string.

A structured element is a **value**, so it cannot carry a control
character: `["printf", "%s\\n", value]` is refused, because a newline inside a
quoted argument is how a second command gets smuggled in. A value that really
needs a newline goes through raw shell text, which is shell source and stays
verbatim.

## QEMU guests

`QEMUHost` supports direct Unix-socket, libvirt, and SSH-tunneled Unix-socket
QGA transports. The SSH form works with a hypervisor exposing per-guest QGA
sockets:

```python
config = QEMUConfig("vm-id", transport="ssh", ssh=SSHConfig("pve", username="root"))
with config as guest:
    result = guest.run(["printf", "%s", "hello"], encoding="utf-8")
```

QGA execution is buffered. It supports argv, Base64 stdin, separate captured
output, exit status, and polling. A timeout cannot cancel the guest process;
`TimeoutExpired.orphaned` is true and its QGA PID is retained when known. QGA
has no native cwd, and its `env` list *replaces* the guest environment instead
of adding to it, so shell commands embed both cwd and env in the rendered
script while direct executable paths reject both.

qemu-ga caps the output it captures, and says so in the status reply.
`result.stdout_truncated` and `result.stderr_truncated` carry that flag, and a
truncated result also raises a `RuntimeWarning` -- a shortened transcript is
otherwise indistinguishable from a complete one. Each guest-agent round trip is
bounded by the config's `timeout.read` (10 s) independently of the command's own
`timeout=`, so an agent that stops answering is reported as such
(`ConnectionError`) rather than as a command that ran too long.
`QEMUConfig.max_reply_size` bounds a single reply; a reply refused for size
takes the captured output with it, so the error names the pid and the setting.

`shell="auto"` and `pathname="auto"` need *positive* evidence of the
guest's family: `guest-get-osinfo`, or a command in `guest-info` that only one
family implements. An agent that provides neither -- `guest-get-osinfo` can be
blocklisted -- makes both raise `NotImplementedError` naming the settings that
resolve it, rather than quietly answering POSIX.

An optional injected `QEMUSerialConsole` exposes raw rescue-console access
through `guest.open_serial()`. It is exclusive, merged-stream, and does not
infer login, shell, prompts, command status, or filesystem behavior; to run
commands on the guest's shell there, drive it with `PosixConsoleProfile`
through `console_stream` (see the serial section above). A live
serial test requires a VM with a separately configured serial device.

!!! note
    SSH needs `hostctl[ssh]`; containers need `hostctl[container]`;
    explicit-credential WinRM needs `hostctl[winrm]`.
    On a Windows client, a password-free WinRM config uses native
    current-context PowerShell remoting, whatever extras are installed.
