# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Protocol base `Host` with explicit `LocalHost`, `SshHost`, and `WinRMHost`
  implementations, normalized `HostInfo`, and capability discovery.
- `HostConfig` connection identity and lifecycle with `LocalConfig`, `SshConfig`,
  and `WinRMConfig`, secret-safe extensible URI dispatch, and context-managed
  connect/close behavior.
- Explicit POSIX or PowerShell command dialects for SSH, independent of the remote
  POSIX or Windows path flavor.
- `pathlib_next.Path` as the shared path contract for local and SFTP-backed paths;
  WinRM deliberately reports no filesystem capability.
- Initial extraction from `pytruenas`: command execution and SFTP filesystem access.
- `LocalExecutor` and native Windows `LocalHost.run()` through PowerShell.
- Optional positive SSH shell detection with `SshConfig(dialect="auto")`.
- Current-context native Windows remoting when `WinRMConfig.password` is omitted.
- `WinRMPath`, a Windows-semantic `pathlib_next.Path` with PowerShell-backed
  metadata, traversal, mutation, and chunked buffered file I/O.
- Subprocess-compatible buffered WinRM output handles and normalized SSH/WinRM
  transport errors.
- Docker Engine `ContainerHost`, buffered exec provider, inspected POSIX/Windows
  shell and path selection, and archive-backed `pathlib_next.Path` file access.
- Persistent SSH and container shell sessions with optional terminal allocation,
  resizing, stream lifecycle, and shell-aware `send(*cmds, cwd=..., env=...)`.
- Raw `SerialExecutor`/`SerialProcess` transport foundation with validated UART
  settings, optional PySerial URLs, exclusive sessions, and break/DTR/RTS controls.
- `QemuHost` with direct Unix, libvirt, and SSH-tunneled QGA transports,
  guest discovery, buffered execution, and POSIX/Windows QGA file paths.
- Explicit raw QEMU serial-console process leases with optional resize support
  and no inferred login, shell, status, or filesystem semantics.

[Unreleased]: https://github.com/jose-pr/hostctl/compare/v0.1.0...HEAD
