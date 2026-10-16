"""Console protocols over any byte stream: logins, prompts and framed commands."""

from ._posix import PosixConsoleProfile as PosixConsoleProfile
from ._profiles import (
    ConsoleProfile as ConsoleProfile,
    LoginStep as LoginStep,
    PromptConsoleProfile as PromptConsoleProfile,
    RawConsoleProfile as RawConsoleProfile,
)
from ._stream import ConsoleStream as ConsoleStream, console_stream as console_stream

__all__ = [
    "ConsoleProfile",
    "ConsoleStream",
    "LoginStep",
    "PosixConsoleProfile",
    "PromptConsoleProfile",
    "RawConsoleProfile",
    "console_stream",
]
