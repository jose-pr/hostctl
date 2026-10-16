"""hostctl -- run commands and access files on a host, local or remote.

Protocol-agnostic host management: concrete hosts expose only the operations
their providers support. The shell and the path grammar stay explicit where
the transport cannot identify them.

``__all__`` is the package's promise: every name here is one a caller must be
able to *write* -- to construct a host or config, to catch an exception, to
annotate its own code, to pass as an argument, or to implement at a documented
extension point. Each name also has a public home module (``hostctl.host``,
``hostctl.transports.ssh``, ``hostctl.exceptions``, ...); every other module
starts with an underscore and is internal.
"""

from __future__ import annotations

try:
    from importlib.metadata import PackageNotFoundError, version as _version

    __version__ = _version("hostctl")
except PackageNotFoundError:  # not installed (e.g. running from a bare checkout)
    __version__ = "0.0.0.dev0"

from .exceptions import (
    BundleError as BundleError,
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
from .executor import (
    ExecutionOptions as ExecutionOptions,
    Executor as Executor,
    ExecutorCapability as ExecutorCapability,
    CommandLike as CommandLike,
)
from .console import (
    ConsoleProfile as ConsoleProfile,
    ConsoleStream as ConsoleStream,
    LoginStep as LoginStep,
    PosixConsoleProfile as PosixConsoleProfile,
    PromptConsoleProfile as PromptConsoleProfile,
    RawConsoleProfile as RawConsoleProfile,
    console_stream as console_stream,
)
from .host import (
    DEFAULT as DEFAULT,
    Host as Host,
    HostConfig as HostConfig,
    HostInfo as HostInfo,
    Timeout as Timeout,
)
from .paths import (
    CompositePosixPath as CompositePosixPath,
    CompositeWindowsPath as CompositeWindowsPath,
)
from .process import Process as Process, TerminalOptions as TerminalOptions
from .provider import (
    ExecutorProvider as ExecutorProvider,
    PathProvider as PathProvider,
    ProviderDetail as ProviderDetail,
    ProviderProbe as ProviderProbe,
    ProviderSelection as ProviderSelection,
    ProviderSelector as ProviderSelector,
    SelectionStep as SelectionStep,
    SessionInitializer as SessionInitializer,
)
from ._extras import has_psrp as has_psrp, has_ssh as has_ssh
from ._registry import host_schemes as host_schemes
from .shell import (
    BASH as BASH,
    CMD as CMD,
    FISH as FISH,
    POSIX_SHELL as POSIX_SHELL,
    POWERSHELL as POWERSHELL,
    PWSH as PWSH,
    ZSH as ZSH,
    Cmdlet as Cmdlet,
    Exec as Exec,
    PSParam as PSParam,
    Shell as Shell,
    ShellFlavour as ShellFlavour,
    ShellOperator as ShellOperator,
    ShellSession as ShellSession,
    register_shell_flavour as register_shell_flavour,
    shell_flavour as shell_flavour,
    shell_flavour_names as shell_flavour_names,
)
from .sync import (
    ProgressReader as ProgressReader,
    host_checksum as host_checksum,
    stat_checksum as stat_checksum,
)
from .system import (
    IOSConfig as IOSConfig,
    IOSHost as IOSHost,
    PosixConfig as PosixConfig,
    PosixHost as PosixHost,
    SystemConfig as SystemConfig,
    SystemHost as SystemHost,
    WindowsConfig as WindowsConfig,
    WindowsHost as WindowsHost,
    register_system_provider as register_system_provider,
    system_provider_names as system_provider_names,
)
from .transports.container import (
    ContainerConfig as ContainerConfig,
    ContainerHost as ContainerHost,
)
from .transports.local import (
    LocalConfig as LocalConfig,
    LocalExecutor as LocalExecutor,
    LocalHost as LocalHost,
    local_providers as local_providers,
)
from .transports.qemu import QEMUConfig as QEMUConfig, QEMUHost as QEMUHost
from .transports.serial import SerialConfig as SerialConfig, SerialHost as SerialHost
from .transports.ssh import SSHConfig as SSHConfig, ssh_providers as ssh_providers
from .transports.winrm import (
    WinRMConfig as WinRMConfig,
    winrm_providers as winrm_providers,
)
from .uri import (
    ConnectionString as ConnectionString,
    parse_credentials as parse_credentials,
    redact_uri as redact_uri,
    strict_uri_credentials as strict_uri_credentials,
    strict_uri_query as strict_uri_query,
    uri_host as uri_host,
    uri_hostname as uri_hostname,
)

__all__ = [
    # Hosts and configuration
    "Host",
    "HostConfig",
    "HostInfo",
    "Timeout",
    "DEFAULT",
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
    "CommandLike",
    "LocalExecutor",
    "has_psrp",
    "has_ssh",
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
    # Consoles
    "ConsoleProfile",
    "ConsoleStream",
    "RawConsoleProfile",
    "PromptConsoleProfile",
    "PosixConsoleProfile",
    "LoginStep",
    "console_stream",
    # Provider composition
    "ExecutorProvider",
    "PathProvider",
    "ProviderDetail",
    "ProviderProbe",
    "ProviderSelection",
    "ProviderSelector",
    "SelectionStep",
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
    "BundleError",
    # Transfer helpers
    "ProgressReader",
    "host_checksum",
    "stat_checksum",
    "__version__",
]
