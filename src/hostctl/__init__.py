"""hostctl -- run commands and access files on a host, local or remote.

Protocol-agnostic host management: concrete hosts expose only the operations
their providers support. Shell dialect and path flavor remain explicit where
the transport cannot identify them.

``__all__`` is the package's promise: every name here is one a caller must be
able to *write* -- to construct a host or config, to catch an exception, to
annotate its own code, to pass as an argument, or to implement at a documented
extension point. Concrete backends, transport adapters, and result types the
library only ever hands back are reachable from their defining submodules
(``hostctl.host.qemu``, ``hostctl.provider.transports``, ``hostctl.process``,
and so on) but are not part of the stable surface.
"""

from __future__ import annotations

try:
    from importlib.metadata import PackageNotFoundError, version as _version

    __version__ = _version("hostctl")
except PackageNotFoundError:  # not installed (e.g. running from a bare checkout)
    __version__ = "0.0.0.dev0"

from .executor import (
    Executor as Executor,
    ExecutorCapability as ExecutorCapability,
    ExecutorCommand as ExecutorCommand,
    ExecutionOptions as ExecutionOptions,
    LocalExecutor as LocalExecutor,
    pypsrp_available as pypsrp_available,
    require_pypsrp as require_pypsrp,
)
from .host import (
    ContainerConfig as ContainerConfig,
    ContainerHost as ContainerHost,
    Host as Host,
    HostConfig as HostConfig,
    HostInfo as HostInfo,
    HostPath as HostPath,
    LocalConfig as LocalConfig,
    LocalHost as LocalHost,
    parse_credentials as parse_credentials,
    redact_uri as redact_uri,
    QemuConfig as QemuConfig,
    QemuHost as QemuHost,
    SshConfig as SshConfig,
    SerialConfig as SerialConfig,
    SerialHost as SerialHost,
    WinRMConfig as WinRMConfig,
    WinRMPath as WinRMPath,
    SystemConfig as SystemConfig,
    SystemHost as SystemHost,
    PosixConfig as PosixConfig,
    PosixHost as PosixHost,
    WindowsConfig as WindowsConfig,
    WindowsHost as WindowsHost,
    register_system_provider as register_system_provider,
    IosConfig as IosConfig,
    IosHost as IosHost,
    CompositePosixPath as CompositePosixPath,
    CompositeWindowsPath as CompositeWindowsPath,
)
from .provider import (
    ExecutorProvider as ExecutorProvider,
    OperationNotStarted as OperationNotStarted,
    PathProvider as PathProvider,
    ProviderProbe as ProviderProbe,
    ProviderSelection as ProviderSelection,
    ProviderSelector as ProviderSelector,
    SessionInitializer as SessionInitializer,
)
from .executor._qga import (
    QgaCommandError as QgaCommandError,
    QgaProtocolError as QgaProtocolError,
)
from .process import (
    Process as Process,
    TerminalOptions as TerminalOptions,
)
from .serial import (
    ConsoleProtocolError as ConsoleProtocolError,
    LoginStep as LoginStep,
    PromptConsoleProfile as PromptConsoleProfile,
    RawConsoleProfile as RawConsoleProfile,
    SerialConsoleProtocol as SerialConsoleProtocol,
)
from .sync import (
    ProgressReader as ProgressReader,
    host_checksum as host_checksum,
    stat_checksum as stat_checksum,
)
from .shell import (
    BASH as BASH,
    CMD as CMD,
    FISH as FISH,
    POWERSHELL as POWERSHELL,
    POSIX_SHELL as POSIX_SHELL,
    PWSH as PWSH,
    ZSH as ZSH,
    Shell as Shell,
    ShellFlavour as ShellFlavour,
    ShellOperator as ShellOperator,
    ShellSession as ShellSession,
    register_shell_flavour as register_shell_flavour,
    shell_flavour as shell_flavour,
)

__all__ = [
    # Hosts and configuration
    "Host",
    "HostConfig",
    "HostInfo",
    "HostPath",
    "LocalConfig",
    "LocalHost",
    "parse_credentials",
    "redact_uri",
    "SshConfig",
    "WinRMConfig",
    "WinRMPath",
    "ContainerConfig",
    "ContainerHost",
    "QemuConfig",
    "QemuHost",
    "SerialConfig",
    "SerialHost",
    "SystemConfig",
    "SystemHost",
    "PosixConfig",
    "PosixHost",
    "WindowsConfig",
    "WindowsHost",
    "IosConfig",
    "IosHost",
    # Executors and processes
    "Executor",
    "ExecutionOptions",
    "ExecutorCapability",
    "ExecutorCommand",
    "LocalExecutor",
    "pypsrp_available",
    "require_pypsrp",
    "Process",
    "TerminalOptions",
    "QgaCommandError",
    "QgaProtocolError",
    # Shells and sessions
    "Shell",
    "ShellFlavour",
    "ShellOperator",
    "ShellSession",
    "register_shell_flavour",
    "shell_flavour",
    "BASH",
    "CMD",
    "FISH",
    "POWERSHELL",
    "POSIX_SHELL",
    "PWSH",
    "ZSH",
    # Serial consoles
    "SerialConsoleProtocol",
    "RawConsoleProfile",
    "PromptConsoleProfile",
    "LoginStep",
    "ConsoleProtocolError",
    # Provider composition
    "ExecutorProvider",
    "PathProvider",
    "ProviderProbe",
    "ProviderSelection",
    "ProviderSelector",
    "OperationNotStarted",
    "SessionInitializer",
    "register_system_provider",
    "CompositePosixPath",
    "CompositeWindowsPath",
    # Transfer helpers
    "ProgressReader",
    "host_checksum",
    "stat_checksum",
    "__version__",
]
