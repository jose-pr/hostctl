"""The POSIX, Windows and IOS system hosts and configs."""

from __future__ import annotations


from ..shell import POWERSHELL, POSIX_SHELL
from ._config import SystemConfig
from ._host import SystemHost


class PosixHost(SystemHost):
    """A POSIX system: POSIX-shell command construction and POSIX paths.

    Compose it from providers directly, or open an :class:`SSHConfig`.
    """

    system_family = "posix"
    default_shell = POSIX_SHELL


class WindowsHost(SystemHost):
    """A Windows system: PowerShell command construction and Windows paths.

    Compose it from providers directly, or open a :class:`WinRMConfig`.
    """

    system_family = "windows"
    default_shell = POWERSHELL


class IOSHost(SystemHost):
    """A Cisco IOS-style device: session and command only, by design.

    It configures **no shell flavour and no path grammar**, which is a
    deliberate gap rather than an unfinished one: IOS command syntax is
    not a shell, and quoting it as one would corrupt ordinary commands.
    So commands must be :class:`Exec` (or raw text the device
    understands), and :meth:`path` raises ``NotImplementedError`` until
    an IOS path grammar exists.
    """

    system_family = "ios"
    default_shell = None


class PosixConfig(SystemConfig, schemes=("posix", "posix+ssh")):
    """Connection identity for a :class:`PosixHost` (`posix:`, `posix+ssh:`)."""

    host_type = PosixHost
    uri_scheme = "posix"


class WindowsConfig(SystemConfig, schemes=("windows", "windows+winrm")):
    """Connection identity for a :class:`WindowsHost` (`windows:`,
    `windows+winrm:`)."""

    host_type = WindowsHost
    uri_scheme = "windows"


class IOSConfig(SystemConfig, schemes=("ios",)):
    """Connection identity for an :class:`IOSHost` (`ios:`), which is
    session- and command-only."""

    host_type = IOSHost
    uri_scheme = "ios"


# Each host family and its configuration are mutually referential, so the
# host -> config direction is bound here, once both sides are defined.
PosixHost.config_type = PosixConfig

WindowsHost.config_type = WindowsConfig

IOSHost.config_type = IOSConfig
