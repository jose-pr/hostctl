"""The host and configuration contracts every transport implements."""

from ._host import Host as Host
from ._config import HostConfig as HostConfig
from ._info import HostInfo as HostInfo

from ._selection import (
    PathnameSelection as PathnameSelection,
    ShellSelection as ShellSelection,
)

__all__ = ["Host", "HostConfig", "HostInfo", "PathnameSelection", "ShellSelection"]
