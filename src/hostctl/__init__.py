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
)
from .transports.local._executor import LocalExecutor as LocalExecutor
from .transports.winrm._psrp import (
    pypsrp_available as pypsrp_available,
    require_pypsrp as require_pypsrp,
)
from .transports.container._host import (
    ContainerConfig as ContainerConfig,
    ContainerHost as ContainerHost,
)
from .uri._connection import ConnectionString as ConnectionString
from .shell._grammar import Cmdlet as Cmdlet, Exec as Exec, PSParam as PSParam
from .host._contracts import (
    Host as Host,
    HostConfig as HostConfig,
    HostInfo as HostInfo,
)
from pathapi import Path as HostPath
from .transports.local._host import (
    LocalConfig as LocalConfig,
    LocalHost as LocalHost,
    local_providers as local_providers,
)
from .uri._parse import (
    parse_credentials as parse_credentials,
    redact_uri as redact_uri,
    strict_uri_credentials as strict_uri_credentials,
    strict_uri_query as strict_uri_query,
    uri_host as uri_host,
    uri_hostname as uri_hostname,
)
from .transports.ssh._transport import (
    ssh_providers as ssh_providers,
    SSHConfig as SSHConfig,
)
from .transports.winrm._transport import (
    winrm_providers as winrm_providers,
    WinRMConfig as WinRMConfig,
    WinRMPath as WinRMPath,
)
from .transports.qemu._host import QEMUConfig as QEMUConfig, QEMUHost as QEMUHost
from .transports.serial._host import (
    SerialConfig as SerialConfig,
    SerialHost as SerialHost,
)
from .system._system import (
    SystemConfig as SystemConfig,
    SystemHost as SystemHost,
    PosixConfig as PosixConfig,
    PosixHost as PosixHost,
    WindowsConfig as WindowsConfig,
    WindowsHost as WindowsHost,
    register_system_provider as register_system_provider,
    system_provider_names as system_provider_names,
    IOSConfig as IOSConfig,
    IOSHost as IOSHost,
)
from ._registry import host_schemes as host_schemes
from .paths._composite import (
    CompositePosixPath as CompositePosixPath,
    CompositeWindowsPath as CompositeWindowsPath,
)
from .provider._contracts import (
    ExecutorProvider as ExecutorProvider,
    PathProvider as PathProvider,
    ProviderProbe as ProviderProbe,
    ProviderSelection as ProviderSelection,
    ProviderSelector as ProviderSelector,
    SessionInitializer as SessionInitializer,
)
from .exceptions import (
    ConsoleProtocolError as ConsoleProtocolError,
    HostctlError as HostctlError,
    OperationNotStartedError as OperationNotStartedError,
    ProtocolError as ProtocolError,
    QGACommandError as QGACommandError,
    QGADisconnectedError as QGADisconnectedError,
    QGAProtocolError as QGAProtocolError,
    QGATimeoutError as QGATimeoutError,
    TransportError as TransportError,
)
from .process import Process as Process, TerminalOptions as TerminalOptions
from .transports.serial._console import (
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
    register_shell_flavour as register_shell_flavour,
    shell_flavour as shell_flavour,
    shell_flavour_names as shell_flavour_names,
)
from .shell._flavour import (
    Shell as Shell,
    ShellFlavour as ShellFlavour,
    ShellOperator as ShellOperator,
    ShellSession as ShellSession,
)

__all__ = [
    # Hosts and configuration
    "Host",
    "HostConfig",
    "HostInfo",
    "HostPath",
    "LocalConfig",
    "LocalHost",
    "local_providers",
    "parse_credentials",
    "redact_uri",
    "ssh_providers",
    "strict_uri_credentials",
    "strict_uri_query",
    "ConnectionString",
    "uri_host",
    "uri_hostname",
    "winrm_providers",
    "SSHConfig",
    "WinRMConfig",
    "WinRMPath",
    "ContainerConfig",
    "ContainerHost",
    "QEMUConfig",
    "QEMUHost",
    "SerialConfig",
    "SerialHost",
    "SystemConfig",
    "SystemHost",
    "PosixConfig",
    "PosixHost",
    "WindowsConfig",
    "WindowsHost",
    "IOSConfig",
    "IOSHost",
    # Executors and processes
    "Cmdlet",
    "Exec",
    "PSParam",
    "Executor",
    "ExecutionOptions",
    "ExecutorCapability",
    "ExecutorCommand",
    "LocalExecutor",
    "pypsrp_available",
    "require_pypsrp",
    "Process",
    "TerminalOptions",
    # Shells and sessions
    "Shell",
    "ShellFlavour",
    "ShellOperator",
    "ShellSession",
    "register_shell_flavour",
    "shell_flavour",
    "shell_flavour_names",
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
    # Provider composition
    "ExecutorProvider",
    "PathProvider",
    "ProviderProbe",
    "ProviderSelection",
    "ProviderSelector",
    "SessionInitializer",
    "register_system_provider",
    "system_provider_names",
    "host_schemes",
    "CompositePosixPath",
    "CompositeWindowsPath",
    # Exceptions
    "HostctlError",
    "TransportError",
    "OperationNotStartedError",
    "QGADisconnectedError",
    "ProtocolError",
    "QGAProtocolError",
    "ConsoleProtocolError",
    "QGATimeoutError",
    "QGACommandError",
    # Transfer helpers
    "ProgressReader",
    "host_checksum",
    "stat_checksum",
    "__version__",
]
