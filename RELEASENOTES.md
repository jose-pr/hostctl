# Release Notes

Detailed notes per release: the narrative, migration detail too fine for the
changelog, the performance story, and the validation evidence behind each
version. `CHANGELOG.md` stays terse and user-facing; this file is the durable
record. Earlier releases are recorded in `CHANGELOG.md` only.

---

## [Unreleased]

## [0.4.0] - 2026-10-19

The 0.4.0 notes. Next performance target: a CI benchmark result for this
release in `benchmarks/results/`, so the following release has a
previous->current table; `host_checksum()` and composite-path dispatch are the
paths worth watching.

### What changed

0.4.0 breaks the documented API, deliberately and once, so a consumer
migrates from one table. Three things move together:

- **Paths are `pathapi` paths.** `pathlib_next` is replaced by `pathapi`
  (`>=0.1.0,<0.2`), and the SSH path leg by `sftppath`. Every `host.path()`
  is a `pathapi.Path`; a provider-backed host returns a `CompositePosixPath`
  or `CompositeWindowsPath` over the transport's backend, so annotate with
  `pathapi.Path` rather than a backend class.
- **The command line is an extra.** `pip install "hostctl[cli]"` adds `duho`;
  the command moves from `hostctl._cli` to `hostctl.cli`, serves every
  subcommand over MCP under `HOSTCTL_MCP=stdio`, and requires `--` before
  `run`'s program.
- **The package is reorganised.** Each transport is one package under
  `hostctl.transports`, the contracts have one home each (`hostctl.host`,
  `.uri`, `.system`, `.provider`, `.executor`, `.process`, `.paths`, `.shell`,
  `.exceptions`), every public module declares `__all__`, and everything else
  is underscore-private. Names follow one convention: acronyms in capitals
  (`SSHConfig`, `QEMUHost`), exceptions ending in `Error` under one base
  (`HostctlError`), and one keyword vocabulary across configs (`shell=`,
  `pathname=`, `timeout=`, keyword-only after the target).

The library's own logger names now follow its module paths
(`hostctl.transports.ssh._transport`); configure the `hostctl` logger, whose
children are not API. `import hostctl` changes no stdlib module and imports
neither netimps nor any extra.

### Migration

Start from the table in `CHANGELOG.md`, "Migrating from 0.3": every row ships
in 0.4.0 and no old name is kept as an alias. The rows that change behaviour,
not only spelling:

- **`except RuntimeError` no longer catches a declined operation.**
  `OperationNotStartedError` (was `OperationNotStarted`) is a
  `ConnectionError`, so an `except ConnectionError` retry around a refused SSH
  port now catches it, which is safe: it is raised only before anything was
  sent. Audit `except OSError` blocks too, since `ConnectionError` is an
  `OSError`.
- **Positional config arguments after the target raise `TypeError`.**
  `SSHConfig("h", 2222)` must become `SSHConfig("h", port=2222)`. A static
  type checker still reads each dataclass's generated `__init__` (the 3.9
  floor has no `kw_only`), so the error appears at run time.
- **Renamed URI query keys raise.** `?dialect=`, `?path_flavor=`, WinRM's
  `?transport=`, `?ssl=`, `?server_cert_validation=`,
  `?operation_timeout_sec=`/`?read_timeout_sec=` and the container's `?user=`
  raise `unknown connection parameter`; a stored URI written by 0.3 must be
  rewritten with the new keys (`shell`, `pathname`, `auth`, `tls`,
  `verify=true|false`, `username`) and without the WinRM timeout keys, which
  have no per-request replacement (`timeout_connect`, `timeout` instead).
  QEMU's `?agent_timeout=` is `?timeout_read=`, and serial's
  `?read_timeout=`/`?write_timeout=` are `?timeout_read=`/`?timeout_write=`.
  `str(config)` of a 0.4.0 config is already in the new form.
- **Every config spells its timeouts as `timeout=`.** A number or `None` is
  the total, the default of `run(timeout=)`; `Timeout(total, connect=,
  read=, write=)` names the rest, and a field the transport cannot honour
  raises instead of being ignored. `QemuConfig(agent_timeout=5)` is
  `timeout=Timeout(read=5)` (a bare `timeout=5` would bound the command, not
  the agent reply), and `SerialConfig(read_timeout=, write_timeout=)` is
  `timeout=Timeout(read=, write=)`. `run(timeout=None)` is no limit for the
  call on every host.
- **WinRM has two timeouts, float seconds, and `verify` is a bool.**
  `server_cert_validation="ignore"` is `verify=False`. `read_timeout_sec` and
  `operation_timeout_sec` are gone, and the per-request timing is internal:
  lower `timeout.connect` (default 30 s) to stop a hang while connecting.
  A native-leg user who raised `read_timeout_sec` so long commands could
  finish raises the total instead (`WinRMConfig(..., timeout=600)`): its
  30 s default applies to `run(timeout=)` on all three legs, so a pywinrm or
  PSRP command past 30 s now raises `TimeoutExpired` where it used to run
  unbounded.
- **SSH defaults changed.** `username=None` is the ssh_config's `User`,
  else the local account, not `root`, and `port=None` its `Port`, else 22;
  commands and paths both read `~/.ssh/config`, as `ssh` does, where 0.3's
  paths read none (`ssh_config=None` reads none, and a `str` there is
  configuration text, never a file name); a relative `host.path()` is the
  login directory; `connect()` raises
  `OperationNotStartedError` when no provider connected; commands and paths
  share one connection and one login, so a server's per-connection limits
  and authentication logs see one where they saw two.
- **Removed constructors.** `PosixHost.from_ssh()`/`WindowsHost.from_winrm()`
  are `config.open()`; `WinRMPath` and `HostPath` leave `hostctl`.
- **Logger names.** A logging configuration naming `hostctl.host.ssh`,
  `hostctl.provider` or another child configures nothing now; configure
  `hostctl`.
- **netimps 0.4.** `NETIMPS_NO_SOCKET_PATCH` makes netimps refuse to import;
  use `NETIMPS_SOCKET_PATCH=0`.

**Downstream: pytruenas.** pytruenas 0.5.4 and earlier pin `hostctl<0.4` and
import 0.3 paths (`from hostctl.host import PosixHost, SshConfig, ...`), so
they do not import against 0.4.0. The `downstream-pytruenas` job of
`test.yml`, which installs this checkout over pytruenas, fails at collection
until pytruenas ports to the 0.4.0 names; the job is informative and does not
gate the release.

### Benchmarks

None taken for this release: `benchmarks/results/` holds 0.2.7 and 0.3.0
results only, and no CI benchmark exists for 0.3.2 or 0.4.0, so there is no
previous->current table.

### Validation

- Suite at the commit that prepared these notes: 2885 passed / 117 skipped on
  Python 3.14, 2881 passed / 121 skipped on the 3.9 floor (Windows ARM64); the
  skips are transports, programs and live endpoints absent on that machine.
- `black --check src tests` and `mkdocs build --strict` clean.
- Every signature in the shipped API header is compared with the code
  (`tests/test_packaging.py`), and the wheel and sdist contents are checked
  for the header and against private files.
- No commit message carries an agent attribution trailer, and no tracked file
  refers to private working notes.

### Publication state

Prepared; the `v0.4.0` tag awaits the maintainer's consent.
