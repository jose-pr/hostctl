# Running commands

Concrete hosts implement `Host.run(...)` where their transport can support it:
`LocalHost` uses POSIX sh or Windows PowerShell according to the local platform,
`SshHost` uses `asyncssh`, and `WinRMHost` uses PowerShell through `pywinrm` or
the current Windows security context. `ContainerHost` uses Docker Engine exec,
and `QemuHost` uses QEMU Guest Agent `guest-exec`.

```python
result = host.run("echo hello")
print(result.stdout)
```

`run()` returns a `subprocess.CompletedProcess`. Highlights:

- Multiple positional commands are joined with `;`. A command given as a
  `list`/`tuple` is shell-quoted piece by piece.
- `capture_output` may be `True` (both streams), `"stdout"`, `"stderr"`, or
  `False`.
- SSH `input=`, `cwd=`, `env=`, `check=`, `timeout=`, and
  `encoding=`/`errors=` follow the subprocess-shaped contract.
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
    session.send(["printf", "%s\n", "quoted value"])
    session.send("echo raw | sed s/raw/stream/")
    output = session.read()
```

`session.send(*cmds, cwd=..., env=...)` accepts the same raw strings, structured
argument lists, multiple commands, paths, and `ShellOperator` values as `run()`.
Changing directory or environment inside the session persists in that shell.
`TerminalOptions` selects the terminal type and initial size; `resize()` changes
it later. TTY sessions combine stdout and stderr.

The streaming interface deliberately exposes `send()`, `read()`, and
`read_stderr()` rather than pretending that one read corresponds to one command.
Command-correlated capture requires a separate framing protocol.

WinRM does not advertise sessions. A future PSRP provider can expose persistent
PowerShell runspaces, but a runspace is not a TTY and `pywinrm` is buffered.

`SerialConfig("serial:///...")` provides an exclusive serial host. Its default
`RawConsoleProfile` exposes `host.shell.session()` with a merged byte stream,
serial break, DTR, and RTS controls, but no filesystem or command status. A
`PromptConsoleProfile(prompt=..., status_marker=..., reliable_status=True)` can
opt into framed `host.run()` after defining the device's prompt and completion
marker. Login steps and credentials are supplied programmatically and are never
placed in the URI. Device names are opaque (native ports, `loop://`, `socket://`,
and RFC 2217 URLs are passed to PySerial); RFC 2217 provides no encryption or
authentication and must be protected by an external secure transport.

## QEMU guests

`QemuHost` supports direct Unix-socket, libvirt, and SSH-tunneled Unix-socket
QGA transports. The SSH form works with a hypervisor exposing per-guest QGA
sockets:

```python
config = QemuConfig("vm-id", transport="ssh", ssh=SshConfig("pve", username="root"))
with config as guest:
    result = guest.run(["printf", "%s", "hello"], encoding="utf-8")
```

QGA execution is buffered. It supports argv, environment, Base64 stdin,
separate captured output, exit status, and polling. A timeout cannot cancel the
guest process; `TimeoutExpired.orphaned` is true and its QGA PID is retained
when known. QGA has no native cwd, so shell commands embed cwd while direct
executable paths reject it.

An optional injected `QemuSerialConsole` exposes raw rescue-console access
through `guest.open_serial()`. It is exclusive, merged-stream, and does not
infer login, shell, prompts, command status, or filesystem behavior. A live
serial test requires a VM with a separately configured serial device.

!!! note
    SSH needs `hostctl[ssh]`; containers need `hostctl[container]`;
    explicit-credential WinRM needs `hostctl[winrm]`.
    On Windows, a password-free WinRM config uses native current-context
    PowerShell remoting.
