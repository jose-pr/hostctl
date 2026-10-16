"""Hosts composed from ordered executor and path providers."""

from ._kinds import (
    IOSConfig as IOSConfig,
    IOSHost as IOSHost,
    PosixConfig as PosixConfig,
    PosixHost as PosixHost,
    WindowsConfig as WindowsConfig,
    WindowsHost as WindowsHost,
)
from ._config import SystemConfig as SystemConfig
from ._host import SystemHost as SystemHost
from ._resolvers import (
    register_system_provider as register_system_provider,
    system_provider_names as system_provider_names,
)

__all__ = [
    "IOSConfig",
    "IOSHost",
    "PosixConfig",
    "PosixHost",
    "SystemConfig",
    "SystemHost",
    "WindowsConfig",
    "WindowsHost",
    "register_system_provider",
    "system_provider_names",
]
