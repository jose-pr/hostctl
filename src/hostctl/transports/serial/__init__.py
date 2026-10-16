"""Serial lines: raw consoles and prompt-framed command profiles."""

from ._console import (
    LoginStep as LoginStep,
    PromptConsoleProfile as PromptConsoleProfile,
    RawConsoleProfile as RawConsoleProfile,
    ConsoleProfile as ConsoleProfile,
)
from ._executor import SerialFactory as SerialFactory, SerialLike as SerialLike
from ._host import SerialConfig as SerialConfig, SerialHost as SerialHost
from ._process import SerialProcess as SerialProcess

__all__ = [
    "LoginStep",
    "PromptConsoleProfile",
    "RawConsoleProfile",
    "SerialConfig",
    "ConsoleProfile",
    "SerialFactory",
    "SerialHost",
    "SerialLike",
    "SerialProcess",
]
