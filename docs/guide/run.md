# Running commands

Concrete hosts implement `Host.run(...)` where their transport can support it:
`LocalHost` uses POSIX sh or Windows PowerShell according to the local platform,
`PosixHost.from_ssh(SshConfig(...))` uses `asyncssh`, and
`WindowsHost.from_winrm(WinRMConfig(...))` uses PowerShell through `pywinrm` or
the current Windows security context. `ContainerHost` uses Docker Engine exec,
and `QemuHost` uses QEMU Guest Agent `guest-exec`.

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
exit $(if ($?) { 0 } elseif ($LASTEXITCODE) { $LASTEXITCODE } else { 1 })
```

Both channels are consulted because `$LASTEXITCODE` is set only by *native*
commands: a failing cmdlet leaves it `$null` (so `exit $LASTEXITCODE` reported
success), and it goes stale, so a native failure followed by a successful
cmdlet reported the old code. The epilogue is on its own line so a script
ending in a `#` comment cannot swallow it.

`run()` returns a `subprocess.CompletedProcess`. Highlights:

- `capture_output` may be `True` (both streams), `"stdout"`, `"stderr"`, or
  `False`.
- **`check` defaults to `True`**, unlike `subprocess.run`. A non-zero
  status raises `CalledProcessError` unless you pass `check=False`; a remote
  command that fails is an error by default rather than a return value to
  inspect.
- SSH `input=`, `cwd=`, `env=`, `timeout=` and `encoding=`/`errors=` follow
  the subprocess-shaped contract.
- WinRM buffers output to caller-owned file handles. It cannot stream stdin or
  select another executable. `read_timeout_sec` is a transport-read setting,
  not a total command deadline; native current-context remoting does not kill
  a remote command when that read window elapses. Remote exit codes are
  preserved, while transport/authentication failures are normalized to
  `ConnectionError`/`PermissionError`.
- SSH command dialect may be explicit (`POSIX_SHELL`, `POWERSHELL`, etc.) or
  positively detected with `SshConfig(dialect="auto")`. Structured commands,
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

Defaults apply wherever the shell builds a script. `Shell.execute()` used
directly against an executor with no native `cwd`/`env` support dispatches one
opaque command and does not receive them.

`session.send(*cmds, cwd=..., env=...)` accepts the same raw strings, structured
argument lists, multiple commands, paths, and `ShellOperator` values as `run()`.
Changing directory or environment inside the session persists in that shell.
`TerminalOptions` selects the terminal type and initial size; `resize()` changes
it later. TTY sessions combine stdout and stderr.

The streaming interface deliberately exposes `send()`, `read()`, and
`read_stderr()` rather than pretending that one read corresponds to one command.
Command-correlated capture requires a separate framing protocol.

WinRM's default provider is `auto`: it selects the optional PSRP provider when
`hostctl[psrp]` is installed on Python 3.10+, otherwise it falls back to
`pywinrm`. Select `provider="psrp"` to require PSRP explicitly. PSRP exposes a
persistent typed runspace rather than a TTY:

```python
with WinRMConfig("windows.example.com", "admin", "secret", provider="psrp") as host:
    with host.runspace() as session:
        session.invoke("$x = 1")
        result = session.invoke("$x")
        print(result.output, result.streams.error)
```

Install `hostctl[psrp]` for Python 3.10+; Python 3.9 remains on the portable
buffered `pywinrm` provider.

A serial host has two entry points, and they take different first arguments.
The constructor takes the **device**, the way pyserial names it --
`SerialConfig("COM3")`, `SerialConfig("/dev/ttyUSB0")`,
`SerialConfig("rfc2217://host:4001")` -- while the URI dispatcher takes the
**connection URI**, with the device inside it: `Host("serial:///COM3")`.
Passing a URI to the constructor asks pyserial to open a port literally named
`serial:///COM3`, which no system has. Its default
`RawConsoleProfile` exposes `host.shell.session()` with a merged byte stream,
serial break, DTR, and RTS controls, but no filesystem or command status. A
`PromptConsoleProfile(prompt=..., status_marker=..., reliable_status=True)` can
opt into framed `host.run()` after defining the device's prompt and completion
marker. Login steps and credentials are supplied programmatically and are never
placed in the URI.

A framed exchange owns a **window**: it starts after the command the profile
echoed back and ends at the first prompt following the completion marker.
That is what makes framing trustworthy on a stream with no request/response
correlation:

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
  `SerialConfig(read_timeout=None)` host raises `TimeoutExpired` instead of
  blocking inside the driver. Device names are opaque (native ports, `loop://`, `socket://`,
and RFC 2217 URLs are passed to PySerial); RFC 2217 provides no encryption or
authentication and must be protected by an external secure transport.

## What a rendered command may contain

A **raw string** is shell source and stays verbatim, so it may span lines and
carry tabs -- a heredoc, an `if`/`for` block, an embedded `awk` program. Only
NUL and the other non-whitespace control characters are refused there: nothing
in any supported shell needs them, and they defeat terminal and log inspection.
A **structured element** is a value, and a value carrying any control character
at all is refused, because a newline inside a quoted argument is how a second
command gets smuggled in.

In a structured PowerShell command, a token that is exactly a parameter name
(`-LiteralPath`, `-Force`, `-Path:`) is rendered unquoted so the binder reads
it as one; `host.run(["Remove-Item", "-LiteralPath", path])` therefore binds
and deletes. Every other element is a quoted string literal. A value that
happens to look like a parameter name but is meant as data cannot be expressed
through the structured grammar -- render it as raw source, or pass the `--`
separator the target program provides.

A structured element is a **value**, so it cannot carry a control
character: `["printf", "%s\\n", value]` is refused, because a newline inside a
quoted argument is how a second command gets smuggled in. A value that really
needs a newline goes through raw shell text, which is shell source and stays
verbatim.

## QEMU guests

`QemuHost` supports direct Unix-socket, libvirt, and SSH-tunneled Unix-socket
QGA transports. The SSH form works with a hypervisor exposing per-guest QGA
sockets:

```python
config = QemuConfig("vm-id", transport="ssh", ssh=SshConfig("pve", username="root"))
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
bounded by `QemuConfig.agent_timeout` independently of the command's own
`timeout=`, so an agent that stops answering is reported as such
(`ConnectionError`) rather than as a command that ran too long.
`QemuConfig.max_reply_size` bounds a single reply; a reply refused for size
takes the captured output with it, so the error names the pid and the setting.

`dialect="auto"` and `path_flavor="auto"` need *positive* evidence of the
guest's family: `guest-get-osinfo`, or a command in `guest-info` that only one
family implements. An agent that provides neither -- `guest-get-osinfo` can be
blocklisted -- makes both raise `NotImplementedError` naming the settings that
resolve it, rather than quietly answering POSIX.

An optional injected `QemuSerialConsole` exposes raw rescue-console access
through `guest.open_serial()`. It is exclusive, merged-stream, and does not
infer login, shell, prompts, command status, or filesystem behavior. A live
serial test requires a VM with a separately configured serial device.

!!! note
    SSH needs `hostctl[ssh]`; containers need `hostctl[container]`;
    explicit-credential WinRM needs `hostctl[winrm]`.
    On Windows, a password-free WinRM config uses native current-context
    PowerShell remoting.
