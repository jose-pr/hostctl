"""Host contracts and built-in host implementations."""

from ._common import (
    Host as Host,
    HostConfig as HostConfig,
    HostInfo as HostInfo,
    HostPath as HostPath,
)
from ._local import LocalConfig as LocalConfig, LocalHost as LocalHost
from .container import (
    ContainerConfig as ContainerConfig,
    ContainerHost as ContainerHost,
)
from .container_path import (
    ContainerPathBackend as ContainerPathBackend,
    PosixContainerPath as PosixContainerPath,
    WindowsContainerPath as WindowsContainerPath,
)
from .qemu import (
    QemuConfig as QemuConfig,
    QemuHost as QemuHost,
    PosixQemuPath as PosixQemuPath,
    QgaPathBackend as QgaPathBackend,
    WindowsQemuPath as WindowsQemuPath,
)
from ._ssh import (
    PathnameConstructor as PathnameConstructor,
    SshConfig as SshConfig,
)
from ._winrm import (
    WinRMConfig as WinRMConfig,
    WinRMPath as WinRMPath,
    WinRMPathBackend as WinRMPathBackend,
    WinRMProvider as WinRMProvider,
)
from .system import (
    IosConfig as IosConfig,
    IosHost as IosHost,
    PosixConfig as PosixConfig,
    PosixHost as PosixHost,
    SystemConfig as SystemConfig,
    SystemHost as SystemHost,
    WindowsConfig as WindowsConfig,
    WindowsHost as WindowsHost,
    register_system_provider as register_system_provider,
)
from .serial import SerialConfig as SerialConfig, SerialHost as SerialHost
from .composite_path import (
    CompositePosixPath as CompositePosixPath,
    CompositeWindowsPath as CompositeWindowsPath,
)

__all__ = [
    "Host",
    "HostConfig",
    "HostInfo",
    "HostPath",
    "ContainerConfig",
    "ContainerHost",
    "ContainerPathBackend",
    "LocalConfig",
    "LocalHost",
    "PathnameConstructor",
    "PosixContainerPath",
    "PosixQemuPath",
    "QemuConfig",
    "QemuHost",
    "QgaPathBackend",
    "SshConfig",
    "SerialConfig",
    "SerialHost",
    "WinRMConfig",
    "WinRMProvider",
    "WinRMPath",
    "WinRMPathBackend",
    "WindowsContainerPath",
    "WindowsQemuPath",
    "SystemConfig",
    "SystemHost",
    "PosixConfig",
    "PosixHost",
    "WindowsConfig",
    "WindowsHost",
    "register_system_provider",
    "IosConfig",
    "IosHost",
    "CompositePosixPath",
    "CompositeWindowsPath",
]
