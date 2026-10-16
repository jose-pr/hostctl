# hostctl

[![Version](https://img.shields.io/pypi/v/hostctl.svg)](https://pypi.org/project/hostctl/)
[![Python versions](https://img.shields.io/pypi/pyversions/hostctl.svg)](https://pypi.org/project/hostctl/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/jose-pr/hostctl/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-latest-blue.svg)](https://jose-pr.github.io/hostctl/)
[![CI](https://img.shields.io/github/actions/workflow/status/jose-pr/hostctl/test.yml)](https://github.com/jose-pr/hostctl/actions/workflows/test.yml)

A **protocol-agnostic way to run commands and access files on a host**, local
or remote. `Host` defines the operations and `HostConfig` owns secret-safe
connection identity, URI dispatch and lifecycle; SSH, WinRM, containers, QEMU
guests and serial consoles plug in as providers, and a host exposes only the
capabilities its providers support. The guide and API reference are at
<https://jose-pr.github.io/hostctl/>.

## Features

- **`Host.run(...)`** — subprocess-compatible results from local shells,
  SSH (`asyncssh`), or PowerShell over WinRM (`pywinrm`). Two defaults differ
  from `subprocess.run` deliberately: `check` and `capture_output` are both
  `True`. Unsupported transport options raise `NotImplementedError`.
- **`Host.path(...)`** — a `pathapi.Path` filesystem view: local,
  remote SFTP, Windows over WinRM, container archives, or QEMU Guest Agent.
- **Cross-host copy and sync** — use `Path.copy()`/`PathSyncer` directly;
  `host_checksum()` computes unchanged-file digests beside remote data.
- **`with host.shell as session:`** — a persistent shell over SSH or a
  container, closed on exit; `host.shell.session(...)` takes a starting
  command, TTY, `cwd`, `env`, or encoding. `send(*cmds)` uses the same
  structured quoting rules.
- **Serial console hosts.** `SerialConfig` accepts opaque native/PySerial URLs;
  raw profiles provide exclusive sessions, while an explicitly configured
  prompt profile can add safely framed `run()` results. Serial consoles never
  imply a filesystem or PTY and RFC 2217 has no encryption.
- **Explicit or detected SSH shells.** Use a concrete dialect for deterministic
  behavior or `shell="auto"` for positive POSIX/Windows probing.
- **Extensible shell languages.** Select a registered string, a
  `ShellFlavour` subclass, or a configured flavour instance.
- **Extension point, not a closed abstraction.** Override `Host.path()` in a
  subclass to add a project-specific backend (see
  [Extending hostctl](https://jose-pr.github.io/hostctl/guide/extending/)).

## Installation

```bash
pip install hostctl
```

Optional features/extras:

| Extra/flag | Adds | Needed for |
| --- | --- | --- |
| `ssh` | `asyncssh`, `sftppath[asyncssh]` | `run()`/`path()` over SSH |
| `winrm` | `pywinrm` | PowerShell `run()` over WinRM |
| `psrp` | `pypsrp` on Python 3.10+ | persistent typed PowerShell runspaces |
| `container` | Docker SDK for Python | Docker Engine `run()`/`path()`/sessions |
| `serial` | PySerial | raw native/RFC 2217/socket serial sessions |
| `qemu-libvirt` | libvirt Python bindings | local libvirt QGA transport |
| `winrm-credssp` | `pywinrm[credssp]` | WinRM `auth="credssp"` |
| `winrm-kerberos` | `pywinrm[kerberos]` | WinRM `auth="kerberos"` |
| `cli` | `duho` | the `hostctl` command |
| `deploy` | `packaging`, `pathspec` (`tomli` before 3.11) | `hostctl.deploy`: installing a Python program on a host without `pip` |

## Quick start

```python
from hostctl import (
    Host,
    HostConfig,
    ContainerConfig,
    QEMUConfig,
    LocalConfig,
    POWERSHELL,
    SSHConfig,
    WinRMConfig,
)
from pathapi import WindowsPathname

# Local
with LocalConfig() as host:
    result = host.run("echo hello")
    print(result.stdout)

# SSH to a POSIX target (needs the `ssh` extra)
with SSHConfig(host="nas.example.com", username="admin", password="secret") as host:
    result = host.run("df -h")
    for line in host.path("/etc").iterdir():
        print(line)

# SSH to Windows is explicit, not inferred from the transport.
windows_ssh = SSHConfig(
    host="windows.example.com",
    username="admin",
    password="secret",
    shell=POWERSHELL,
    pathname=WindowsPathname,
)

# WinRM supports PowerShell execution and Windows filesystem paths.
with WinRMConfig(
    "windows.example.com", username="admin", password="secret", tls=True
) as windows:
    windows.run(["Write-Output", "hello"])
    windows.path(r"C:\Temp\hello.txt").write_text("hello", encoding="utf-8")

# An existing running container (needs the `container` extra).
with ContainerConfig("application") as container:
    container.run(["printf", "%s", "hello"])
    print(container.path("/etc/os-release").read_text())
    with container.shell.session(terminal=True, encoding="utf-8") as session:
        session.send(["printf", "%s", "hello from the session"])
        print(session.read())

# A QEMU guest through its QGA Unix socket tunneled over SSH.
with QEMUConfig(
    "vm-id",
    transport="ssh",
    ssh=SSHConfig("hypervisor.example", username="root"),
) as guest:
    print(guest.info())
    print(guest.run(["echo", "hello"], encoding="utf-8").stdout)
    print(guest.path("/etc/os-release").read_text())

# A canonical connection URI carries configuration, never a password.
with Host(windows_ssh.connection_uri, password="secret") as same_host:
    same_host.run(["Write-Output", "hello"])

# A `user:password@host` URI is still valid input: the password is extracted
# and used, and never rendered back out. Use `redact_uri` to display one.
with Host("ssh://admin:secret@nas.example.com") as same_host:
    same_host.run("uptime")
```

`run()` takes one positional argument per command, so a `list` is a single
command whose elements are each quoted, not a list of commands:

```python
host.run(["chmod", "755", target])                          # one command
host.run(["chmod", "755", target], ["chown", "u", target])  # two commands
host.run("chmod", "755", target)                            # WRONG: three commands
```

A raw string is verbatim shell text, and `Exec(program, *args)` is direct
execution — one program plus argv, with no shell layer, where the program may
be an absolute path or a bare name resolved through the target's `PATH`. A
`ShellOperator` between two commands joins them conditionally. See the
[running commands guide](https://jose-pr.github.io/hostctl/guide/run/) for the
full rules.

A URI may carry `user:password@host` on the way *in* — it is a valid URI, so
hostctl accepts it, extracts the password into the credentials, and keeps it
out of every rendered form. Passing the same password both in the URI and as an
argument is an error rather than a silent precedence rule. `redact_uri(uri)`
strips the password and returns a valid, reusable URI — it is removed rather
than masked, so a rendered form can never round-trip a wrong credential.

Shell `env` defaults merge per key; `env=None` declines them, leaving whatever
environment the host itself provides.

A password field may also carry extra credentials, one per line after it:
`parse_credentials("hunter2\notp:123456")` yields `("hunter2", {"otp":
"123456"})`. A bare name is a flag meaning the same as `name:`. This lets an
OTP or other second factor reach a transport through a single field — a URI's
userinfo, an environment variable, a prompt — without each caller inventing an
encoding. Inside a URI the separator may be written raw — hostctl encodes it
before parsing, since `urlsplit` would otherwise delete it and silently merge
the extras into the password. A control character in the *host* is still
rejected: no encoding makes a hostname containing one meaningful, and allowing
it would let a URI that reads as one target resolve to another.

`str(config)` is the same canonical, secret-free connection string as
`config.connection_uri`. `HostConfig(str(config), **secrets)` reconstructs the
concrete configuration without creating or connecting a host.

WinRM certificate authentication is not exposed: its certificate and key
configuration is not part of the API.

### Composable system hosts

Use `PosixHost`, `WindowsHost`, or `IOSHost` when system semantics should be
independent of the transport. Providers are tried in declaration order during
preflight; a provider may be retried only when it raises
`OperationNotStartedError`, which guarantees that no remote operation was sent.
`IOSHost` is deliberately session- and command-only: it configures no shell
flavour and no path grammar, so commands must be `Exec(...)` (or raw text the
device understands) and `path()` raises `NotImplementedError` until an IOS
path grammar exists.

Paths retain their selected provider and expose it through `.provider` and
`.via(name)`:

```python
from pathapi import Path

from hostctl import ExecutorProvider, PathProvider, PosixHost, LocalExecutor

host = PosixHost(
    executor_providers=(ExecutorProvider("ssh", ssh_executor),
                        ExecutorProvider("local", LocalExecutor())),
    path_providers=(PathProvider("sftp", sftp_path),
                    PathProvider("rpc", lambda *p: Path(*p))),
)
path = host.path("etc", "hosts")
print(path.provider.name)
```

Application-specific adapters can follow the SFTP/RPC/download pattern in
[`examples/application_provider.py`](https://github.com/jose-pr/hostctl/blob/main/examples/application_provider.py). The
[Systems and providers](https://jose-pr.github.io/hostctl/guide/providers/)
guide covers selection traces, backend pinning, per-operation capabilities,
provider authoring, and the no-replay safety rule in full.

Opening a transport config gives the same kind of host: an `SSHConfig` opens
as a `PosixHost` and a `WinRMConfig` as a `WindowsHost`, each composed of that
transport's providers:

```python
from hostctl import SSHConfig, WinRMConfig

posix = SSHConfig("server.example", username="root").open()
windows = WinRMConfig("server.example", username="admin").open()
```

SSH and WinRM transport implementations are private providers; QEMU's SSH
tunnel takes an `SSHConfig` directly.

## Command line

The `hostctl` command is a thin wrapper over the library, and needs the `cli`
extra:

```console
pip install "hostctl[cli]"
hostctl run local: -- python -c "print('hello')"
hostctl info ssh://server
hostctl cp ./artifact ssh://server:/tmp/artifact
```

Secrets never belong in argv. Use `HOSTCTL_PASSWORD` or the subcommand's
`--ask-password` option. See the
[CLI guide](https://jose-pr.github.io/hostctl/guide/cli/) for all subcommands
and exit statuses. With `HOSTCTL_MCP=stdio` in its environment the same
program serves every subcommand as an MCP tool over stdio.

## API overview

| Module | Purpose |
| --- | --- |
| `hostctl` | Everything a common task needs, re-exported from the modules below |
| `hostctl.host` | `Host`, `HostConfig`, `HostInfo`: the contracts every transport implements |
| `hostctl.exceptions` | `HostctlError` and the exception tree |
| `hostctl.uri` | `ConnectionString` and the URI/credential helpers |
| `hostctl.system` | Hosts composed from ordered providers: `PosixHost`, `WindowsHost`, ... |
| `hostctl.provider` | Executor/path provider contracts and selection |
| `hostctl.executor` | The `Executor` protocol, option types and stream helpers |
| `hostctl.process` | Persistent process and terminal contracts |
| `hostctl.paths` | `CompositePosixPath`, `CompositeWindowsPath` |
| `hostctl.shell` | Shell flavours, command markers and persistent sessions |
| `hostctl.sync` | Remote checksum and byte-progress helpers for `pathapi` |
| `hostctl.deploy` | Bundle a Python program and install it on a POSIX host; needs the `deploy` extra |
| `hostctl.transports.local`, `.ssh`, `.winrm`, `.container`, `.qemu`, `.serial` | One package per transport: its config, host and providers |
| `hostctl.cli` | The `hostctl` command line (`main`); needs the `cli` extra |

## Development

```bash
py -3.14 -m venv .venv/3.14-nt-amd64
.venv/3.14-nt-amd64/Scripts/python -m pip install -e ".[dev,docs]"
.venv/3.14-nt-amd64/Scripts/python -m pytest -q
.venv/3.14-nt-amd64/Scripts/python -m black --check src tests
# The docs gate the release runs, locally:
.venv/3.14-nt-amd64/Scripts/python -m mkdocs build --strict
```

Every extra, so the documented environment is a superset of CI's: without
`psrp` a capability test skips silently, and without `docs` the gate that can
block a release cannot be run at all.

Python 3.14 is the default development interpreter. Python 3.9 is the
supported floor and a change passes there too; create it the same way with
`py -3.9` as `.venv/3.9-nt-amd64`. A venv is named for its interpreter version
and platform (`.venv/3.14-nt-arm64` under a native ARM64 interpreter, where
`cryptography` has no wheel and the `ssh` extra builds it from source, which
needs a Rust toolchain and OpenSSL).

### Releasing

This project follows [Semantic Versioning](https://semver.org/) and keeps a
[`CHANGELOG.md`](https://github.com/jose-pr/hostctl/blob/main/CHANGELOG.md). Pushing a tag matching `v*` triggers the release
workflow: tests and dependency floors → build (tag/version and `twine` checks)
→ installed-wheel smoke → publish, with a strict docs build as a gate.
The docs site itself is deployed by `docs.yml`, which also redeploys on a push
to `main` touching docs sources and on manual dispatch -- so a docs fix reaches
the site without cutting a tag.

To prepare a release, update `pyproject.toml` and move the complete
`[Unreleased]` section to `## [X.Y.Z] - YYYY-MM-DD` in the same commit. Keep
the package version PEP 440-compatible and use the corresponding SemVer tag
(`vX.Y.Z`, or `vX.Y.Z-rc.N` for a prerelease). Then push the commit and tag;
the workflow extracts that changelog section for the GitHub release and
publishes the built artifacts. Leave a fresh empty `[Unreleased]` section for
the next cycle.

## License

MIT — see [LICENSE](https://github.com/jose-pr/hostctl/blob/main/LICENSE).
