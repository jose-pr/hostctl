"""Host contracts and built-in host implementations."""

from ._common import (
    Host as Host,
    HostConfig as HostConfig,
    HostInfo as HostInfo,
    HostPath as HostPath,
)
from .local import LocalConfig as LocalConfig, LocalHost as LocalHost
from .container import (
    ContainerConfig as ContainerConfig,
    ContainerHost as ContainerHost,
)
from .container_path import (
    ContainerPathBackend as ContainerPathBackend,
    PosixContainerPath as PosixContainerPath,
    WindowsContainerPath as WindowsContainerPath,
)
from .qemu import QemuConfig as QemuConfig, QemuHost as QemuHost
from .qemu_path import (
    PosixQemuPath as PosixQemuPath,
    QgaPathBackend as QgaPathBackend,
    WindowsQemuPath as WindowsQemuPath,
)
from .ssh import (
    PathnameConstructor as PathnameConstructor,
    SshConfig as SshConfig,
    SshHost as SshHost,
)
from .winrm import WinRMConfig as WinRMConfig, WinRMHost as WinRMHost
from .serial import SerialConfig as SerialConfig, SerialHost as SerialHost
from .winrm_path import (
    WinRMPath as WinRMPath,
    WinRMPathBackend as WinRMPathBackend,
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
    "SshHost",
    "SerialConfig",
    "SerialHost",
    "WinRMConfig",
    "WinRMHost",
    "WinRMPath",
    "WinRMPathBackend",
    "WindowsContainerPath",
    "WindowsQemuPath",
]
