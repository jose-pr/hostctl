# API Reference

The names below are the public exports from `hostctl.__all__` — everything a
caller has to be able to *write*: a host or config to construct, an exception
to catch, a type to annotate with, an argument to pass, or a contract to
implement.

Concrete backends and result types the library only ever hands back are not
listed here and are not part of the stable surface. They remain importable from
the module that defines them — `hostctl.host.qemu`, `hostctl.host.container_path`,
`hostctl.host._winrm`, `hostctl.process`, `hostctl.executor`, and
`hostctl.provider.transports` — but their names and signatures may change
without a deprecation cycle.

## Hosts and configuration

::: hostctl.Host
::: hostctl.HostConfig
::: hostctl.HostInfo
::: hostctl.LocalConfig
::: hostctl.LocalHost
::: hostctl.SshConfig
::: hostctl.WinRMConfig
::: hostctl.WinRMPath
::: hostctl.ContainerConfig
::: hostctl.ContainerHost
::: hostctl.QemuConfig
::: hostctl.QemuHost
::: hostctl.SerialConfig
::: hostctl.SerialHost
::: hostctl.SystemConfig
::: hostctl.SystemHost
::: hostctl.PosixConfig
::: hostctl.PosixHost
::: hostctl.WindowsConfig
::: hostctl.WindowsHost
::: hostctl.IosConfig
::: hostctl.IosHost

## Executors and processes

::: hostctl.Exec
::: hostctl.Executor
::: hostctl.ExecutionOptions
::: hostctl.LocalExecutor
::: hostctl.Process
::: hostctl.TerminalOptions
::: hostctl.QgaCommandError
::: hostctl.QgaProtocolError

## Shells and sessions

::: hostctl.ShellFlavour
::: hostctl.Shell
::: hostctl.ShellSession
::: hostctl.ShellOperator
::: hostctl.register_shell_flavour
::: hostctl.shell_flavour

## Serial consoles

::: hostctl.SerialConsoleProtocol
::: hostctl.RawConsoleProfile
::: hostctl.PromptConsoleProfile
::: hostctl.LoginStep
::: hostctl.ConsoleProtocolError

## Provider composition

::: hostctl.ExecutorProvider
::: hostctl.PathProvider
::: hostctl.ProviderProbe
::: hostctl.ProviderSelection
::: hostctl.ProviderSelector
::: hostctl.OperationNotStarted
::: hostctl.SessionInitializer
::: hostctl.register_system_provider
::: hostctl.CompositePosixPath
::: hostctl.CompositeWindowsPath

## Connection strings

::: hostctl.ConnectionString
::: hostctl.redact_uri
::: hostctl.parse_credentials
::: hostctl.strict_uri_credentials
::: hostctl.strict_uri_query
::: hostctl.uri_host
::: hostctl.uri_hostname

## Composing transports

::: hostctl.ssh_providers
::: hostctl.winrm_providers

## Transfers and checksums

::: hostctl.ProgressReader
::: hostctl.host_checksum
::: hostctl.stat_checksum

## Aliases and constants

`HostPath` is `pathlib_next.Path` — the type every usable `host.path()` returns,
re-exported so callers can annotate against it without depending on
`pathlib_next` by name.

`ExecutorCommand` (`str | pathlib.PurePath | pathlib_next.Pathname`) is the
command type accepted by `Executor` and `Shell.execute()`.
`ExecutorCapability` is the enum whose `.ARGS`, `.CWD`, and `.ENV` members
declare native executor support. Its members subclass `str`, so
`ExecutorCapability.CWD == "cwd"`: capability sets are compared as strings
throughout, which lets providers advertise transport-specific tokens
(`"runspace"`) alongside the named members in one vocabulary.

`POSIX_SHELL` and `POWERSHELL` are the built-in shell-flavour instances;
`BASH`, `ZSH`, `FISH`, `CMD`, and `PWSH` are the other ready-to-use flavours.
Any of them can be passed wherever a `ShellFlavour` is expected.

`pypsrp_available()` reports whether the optional PSRP extra is importable and
`require_pypsrp()` raises an actionable `ImportError` naming the extra when it
is not. `__version__` contains the installed package version.
