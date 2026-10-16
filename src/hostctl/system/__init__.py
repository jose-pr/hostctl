"""Hosts composed from ordered executor and path providers."""

from ._system import (
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
