[![Version](https://img.shields.io/pypi/v/hostctl.svg)](https://pypi.org/project/hostctl/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-latest-blue.svg)](https://jose-pr.github.io/hostctl/)
[![CI](https://img.shields.io/github/actions/workflow/status/jose-pr/hostctl/release.yml?label=release%20gate)](https://github.com/jose-pr/hostctl/actions/workflows/release.yml)

A **protocol-agnostic way to run commands and access files on a host**.
`Host` defines operations; `HostConfig` owns secret-safe connection identity,
extensible URI dispatch, and lifecycle. Local, SSH, WinRM, and Docker container
hosts expose only the capabilities their transports actually support.

## Features

- **`Host.run(...)`** — subprocess-compatible results from local shells,
  SSH (`asyncssh`), or PowerShell over WinRM (`pywinrm`). Unsupported transport
  options raise `NotImplementedError`.
- **`Host.path(...)`** — a `pathlib_next.Path` filesystem view: local,
  remote SFTP, Windows over WinRM, container archives, or QEMU Guest Agent.
- **`host.shell.session(...)`** — a persistent shell over SSH or a container,
  optionally with a TTY; `send(*cmds)` uses the same structured quoting rules.
- **Serial console hosts.** `SerialConfig` accepts opaque native/PySerial URLs;
  raw profiles provide exclusive sessions, while an explicitly configured
  prompt profile can add safely framed `run()` results. Serial consoles never
  imply a filesystem or PTY and RFC 2217 has no encryption.
- **Explicit or detected SSH shells.** Use a concrete dialect for deterministic
  behavior or `dialect="auto"` for positive POSIX/Windows probing.
- **Extensible shell languages.** Select a registered string, a
  `ShellFlavour` subclass, or a configured flavour instance.
- **Extension point, not a closed abstraction.** Override `Host.path()` in a
  subclass to add a project-specific backend (see `docs/guide/extending.md`).

## Installation

```bash
pip install hostctl
```

Optional features/extras:

| Extra/flag | Adds | Needed for |
| --- | --- | --- |
| `ssh` | `asyncssh`, `pathlib_next[sftp-async]` | `run()`/`path()` over SSH |
| `winrm` | `pywinrm` | PowerShell `run()` over WinRM |
| `container` | Docker SDK for Python | Docker Engine `run()`/`path()`/sessions |
| `serial` | PySerial | raw native/RFC 2217/socket serial sessions |
| `qemu-libvirt` | libvirt Python bindings | local libvirt QGA transport |

## Quick start

```python
from hostctl import (
    Host,
    HostConfig,
    ContainerConfig,
    QemuConfig,
    LocalConfig,
    POWERSHELL,
    SshConfig,
    WinRMConfig,
)
from pathlib_next import WindowsPathname

# Local
with LocalConfig() as host:
    result = host.run("echo hello")
    print(result.stdout)

# SSH to a POSIX target (needs the `ssh` extra)
with SshConfig(host="nas.example.com", username="admin", password="secret") as host:
    result = host.run("df -h")
    for line in host.path("/etc").iterdir():
        print(line)

# SSH to Windows is explicit, not inferred from the transport.
windows_ssh = SshConfig(
    host="windows.example.com",
    username="admin",
    password="secret",
    dialect=POWERSHELL,
    path_flavor=WindowsPathname,
)

# WinRM supports PowerShell execution and Windows filesystem paths.
with WinRMConfig("windows.example.com", "admin", "secret", ssl=True) as windows:
    windows.run(["Write-Output", "hello"])
    windows.path(r"C:\Temp\hello.txt").write_text("hello", encoding="utf-8")

# An existing running container (needs the `container` extra).
with ContainerConfig("application") as container:
    container.run(["printf", "%s\n", "hello"])
    print(container.path("/etc/os-release").read_text())
    with container.shell.session(terminal=True, encoding="utf-8") as session:
        session.send(["printf", "%s\n", "hello from the session"])
        print(session.read())

# A QEMU guest through its QGA Unix socket tunneled over SSH.
with QemuConfig(
    "vm-id",
    transport="ssh",
    ssh=SshConfig("hypervisor.example", username="root"),
) as guest:
    print(guest.info())
    print(guest.run(["echo", "hello"], encoding="utf-8").stdout)
    print(guest.path("/etc/os-release").read_text())

# Connection URIs contain configuration, never passwords.
with Host(windows_ssh.connection_uri, password="secret") as same_host:
    same_host.run(["Write-Output", "hello"])
```

`str(config)` is the same canonical, secret-free connection string as
`config.connection_uri`. `HostConfig(str(config), **secrets)` reconstructs the
concrete configuration without creating or connecting a host.

Install `hostctl[winrm-kerberos]` or `hostctl[winrm-credssp]` when selecting
those WinRM authentication transports. Certificate authentication is not
exposed until its required certificate/key configuration is part of the API.

### Composable system hosts

Use `PosixHost`, `WindowsHost`, or `IosHost` when system semantics should be
independent of the transport. Providers are tried in declaration order during
preflight; a provider may be retried only when it raises
`OperationNotStarted`, which guarantees that no remote operation was sent.
Paths retain their selected provider and expose it through `.provider` and
`.via(name)`:

```python
from hostctl import ExecutorProvider, PathProvider, PosixHost, LocalExecutor, HostPath

host = PosixHost(
    executor_providers=(ExecutorProvider("ssh", ssh_executor),
                        ExecutorProvider("local", LocalExecutor())),
    path_providers=(PathProvider("sftp", sftp_path),
                    PathProvider("rpc", lambda *p: HostPath(*p))),
)
path = host.path("etc", "hosts")
print(path.provider.name)
```

Application-specific adapters can follow the SFTP/RPC/download pattern in
[`examples/application_provider.py`](examples/application_provider.py).

## Command line

The installed `hostctl` command is a thin wrapper over the library:

```console
hostctl run local: -- python -c "print('hello')"
hostctl info ssh://server
hostctl cp ./artifact ssh://server:/tmp/artifact
```

Secrets never belong in argv. Use `HOSTCTL_PASSWORD` or the subcommand's
`--ask-password` option. See the
[CLI guide](https://jose-pr.github.io/hostctl/guide/cli/) for all subcommands
and exit statuses.

## API overview

| Module | Purpose |
| --- | --- |
| `hostctl.host` | Shared contracts and built-in host providers |
| `hostctl.host.container` | `ContainerConfig`, `ContainerHost` |
| `hostctl.host.qemu` | `QemuConfig`, `QemuHost` |
| `hostctl.host.serial` | `SerialConfig`, `SerialHost` |
| `hostctl.provider` | Ordered executor/path provider composition |
| `hostctl.executor` | Transport-specific command executors |
| `hostctl.shell` | Shell flavours and persistent sessions |
| `hostctl._cli` | Dependency-free command-line entry point |

## Development

```bash
py -3.14 -m venv .venv/3.14-nt-amd64
.venv/3.14-nt-amd64/Scripts/python -m pip install -e ".[dev,ssh,winrm,container,serial]"
.venv/3.14-nt-amd64/Scripts/python -m pytest -q
```

Python 3.14 is the default development interpreter. Python 3.9 remains the
supported compatibility floor and should be selected explicitly with
`py -3.9` when running floor-specific checks.

### Releasing

This project follows [Semantic Versioning](https://semver.org/) and keeps a
[`CHANGELOG.md`](CHANGELOG.md). Pushing a tag matching `v*` triggers the release
workflow: test gate → build → publish → docs deploy.

To prepare a release, update `pyproject.toml` and move the complete
`[Unreleased]` section to `## [X.Y.Z] - YYYY-MM-DD` in the same commit. Keep
the package version PEP 440-compatible and use the corresponding SemVer tag
(`vX.Y.Z`, or `vX.Y.Z-rc.N` for a prerelease). Then push the commit and tag;
the workflow extracts that changelog section for the GitHub release and
publishes the built artifacts. Leave a fresh empty `[Unreleased]` section for
the next cycle.

## License

MIT — see [LICENSE](LICENSE).
