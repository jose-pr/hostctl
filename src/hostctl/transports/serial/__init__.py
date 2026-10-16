"""Serial lines: one exclusive byte stream driven by a `hostctl.console` profile."""

from ._executor import SerialFactory as SerialFactory, SerialLike as SerialLike
from ._host import SerialConfig as SerialConfig, SerialHost as SerialHost
from ._process import SerialProcess as SerialProcess

__all__ = [
    "SerialConfig",
    "SerialFactory",
    "SerialHost",
    "SerialLike",
    "SerialProcess",
]
