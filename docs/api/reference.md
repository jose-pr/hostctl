# API Reference

The names below are the public exports from `hostctl.__all__` — everything a
caller has to be able to *write*: a host or config to construct, an exception
to catch, a type to annotate with, an argument to pass, or a contract to
implement.

Each name also has a public home module: `hostctl.host`, `hostctl.exceptions`,
`hostctl.uri`, `hostctl.system`, `hostctl.provider`, `hostctl.executor`,
`hostctl.process`, `hostctl.paths`, `hostctl.shell`, `hostctl.sync`,
`hostctl.cli` and the transport packages `hostctl.transports.local`, `.ssh`,
`.winrm`, `.container`, `.qemu` and `.serial`. Each declares `__all__`; a name
outside it, and every module whose name starts with an underscore, is internal
and may change without a deprecation cycle. Concrete executors, process
adapters and path backends are internal: a host hands them back typed as
`pathapi.Path`, `Process` or `Executor`.

## Hosts and configuration

::: hostctl.Host
::: hostctl.HostConfig
::: hostctl.HostInfo
::: hostctl.Timeout
::: hostctl.DEFAULT
::: hostctl.LocalConfig
::: hostctl.LocalHost
::: hostctl.SSHConfig
::: hostctl.WinRMConfig
::: hostctl.ContainerConfig
::: hostctl.ContainerHost
::: hostctl.QEMUConfig
::: hostctl.QEMUHost
::: hostctl.SerialConfig
::: hostctl.SerialHost
::: hostctl.SystemConfig
::: hostctl.SystemHost
::: hostctl.PosixConfig
::: hostctl.PosixHost
::: hostctl.WindowsConfig
::: hostctl.WindowsHost
::: hostctl.IOSConfig
::: hostctl.IOSHost

## Executors and processes

::: hostctl.Exec
::: hostctl.Executor
::: hostctl.ExecutionOptions
::: hostctl.LocalExecutor

The bases a transport written outside hostctl subclasses; the override
points and a worked example are in
[Writing an executor](../guide/extending.md#writing-an-executor).

::: hostctl.executor.BufferedExecutor
::: hostctl.executor.StreamingExecutor
::: hostctl.executor.ExecutorRequest
::: hostctl.Process
::: hostctl.TerminalOptions

## Shells and sessions

::: hostctl.ShellFlavour
::: hostctl.Shell
::: hostctl.ShellSession
::: hostctl.ShellOperator
::: hostctl.register_shell_flavour
::: hostctl.shell_flavour

## Serial consoles

::: hostctl.ConsoleProfile
::: hostctl.RawConsoleProfile
::: hostctl.PromptConsoleProfile
::: hostctl.LoginStep

## Exceptions

::: hostctl.HostctlError
::: hostctl.TransportError
::: hostctl.OperationNotStartedError
::: hostctl.QGADisconnectedError
::: hostctl.ProtocolError
::: hostctl.QGAProtocolError
::: hostctl.ConsoleProtocolError
::: hostctl.QGATimeoutError
::: hostctl.QGACommandError

## Provider composition

::: hostctl.ExecutorProvider
::: hostctl.PathProvider
::: hostctl.ProviderProbe
::: hostctl.ProviderSelection
::: hostctl.SelectionStep
::: hostctl.ProviderDetail
::: hostctl.ProviderSelector
::: hostctl.SessionInitializer
::: hostctl.register_system_provider
::: hostctl.system_provider_names
::: hostctl.host_schemes
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

Every usable `host.path()` returns a `pathapi.Path`; annotate with that.

`CommandLike` (`str | pathlib.PurePath | pathapi.Pathname`) is the
command type accepted by `Executor`.
`ExecutorCapability` is the enum whose `.ARGS`, `.CWD`, and `.ENV` members
declare native executor support. Its members subclass `str`, so
`ExecutorCapability.CWD == "cwd"`: capability sets are compared as strings
throughout, which lets providers advertise transport-specific tokens
(`"runspace"`) alongside the named members in one vocabulary.

`POSIX_SHELL` and `POWERSHELL` are the built-in shell-flavour instances;
`BASH`, `ZSH`, `FISH`, `CMD`, and `PWSH` are the other ready-to-use flavours.
Any of them can be passed wherever a `ShellFlavour` is expected.

`has_ssh()` and `has_psrp()` report whether the `ssh` and `psrp` extras are
installed, without importing them; a missing extra otherwise raises an
`ImportError` naming it where it is used. `__version__` contains the
installed package version.

`ShellSelection` and `PathnameSelection` (in `hostctl.host`) are what every
config's `shell=` and `pathname=` accept.
