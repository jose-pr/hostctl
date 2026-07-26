# API Reference

The package-level names below are the public exports from `hostctl.__all__`.
The type aliases and constants are included in the list so integrations can
use one stable import surface; concrete classes and protocols are rendered
from their docstrings.

## Hosts and configuration

::: hostctl.Host
::: hostctl.HostConfig
::: hostctl.HostInfo
::: hostctl.LocalConfig
::: hostctl.LocalHost
::: hostctl.SshConfig
::: hostctl.WinRMConfig
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

::: hostctl.Executor
::: hostctl.ExecutionOptions
::: hostctl.LocalExecutor
::: hostctl.SshExecutor
::: hostctl.WinRMExecutor
::: hostctl.NativeWinRMSession
::: hostctl.WinRMProvider
::: hostctl.ContainerExecutor
::: hostctl.QemuExecutor
::: hostctl.SerialExecutor
::: hostctl.PsrpExecutor
::: hostctl.Process
::: hostctl.ContainerProcess
::: hostctl.SshProcess
::: hostctl.SerialProcess
::: hostctl.SerialConsoleProcess
::: hostctl.QemuSerialProcess
::: hostctl.QemuSerialConsole
::: hostctl.TerminalOptions
::: hostctl.RunspaceSession
::: hostctl.PipelineResult
::: hostctl.PipelineStreams

## Shells and sessions

::: hostctl.ShellFlavour
::: hostctl.PosixShellFlavour
::: hostctl.BashShellFlavour
::: hostctl.ZshShellFlavour
::: hostctl.FishShellFlavour
::: hostctl.CmdShellFlavour
::: hostctl.PowerShellFlavour
::: hostctl.Shell
::: hostctl.ShellSession
::: hostctl.ShellCommand
::: hostctl.ShellOperator
::: hostctl.register_shell_flavour
::: hostctl.shell_flavour

## Paths and guest-agent transports

::: hostctl.ProgressReader
::: hostctl.host_checksum
::: hostctl.stat_checksum
::: hostctl.GuestAgentTransport
::: hostctl.UnixSocketGuestAgentTransport
::: hostctl.LibvirtGuestAgentTransport
::: hostctl.SshUnixGuestAgentTransport
::: hostctl.QgaCommandError
::: hostctl.QgaProtocolError
::: hostctl.SerialSettings
::: hostctl.SerialTransport
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
::: hostctl.SessionInitializer
::: hostctl.CompositePosixPath
::: hostctl.CompositeWindowsPath

## Public aliases and constants

`HostPath`, `WinRMPath`, `ContainerPathBackend`, `PosixContainerPath`,
`WindowsContainerPath`, `QgaPathBackend`, `PosixQemuPath`, `WindowsQemuPath`,
`CommandArgument`, `ExecutorCommand`, `CaptureOutput`, `Environment`,
`FileHandle`, `Input`, `PathLike`, `PathnameConstructor`, `ProcessData`,
`ShellFlavourSelection`, `ShellToken`, and `TerminalRequest` are typing aliases
re-exported from `hostctl`. `BASH`, `CMD`, `FISH`, `POWERSHELL`, `POSIX_SHELL`,
`PWSH`, and `ZSH` are ready-to-use shell-flavour instances. `ExecutorCapability`
and `ShellOperator` are enums; `SerialSettings` and `ProcessData` describe
transport and result metadata. `__version__` contains the installed package
version.
