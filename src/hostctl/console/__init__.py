"""Console protocols over any byte stream: logins, prompts and framed commands.

`ConsoleExecutor` puts the framed POSIX protocol behind an executor, so a
console reached any way (a web shell, a QEMU console) can serve `run()`.
"""

from ._executor import ConsoleExecutor as ConsoleExecutor
from ._posix import PosixConsoleProfile as PosixConsoleProfile
from ._profiles import (
    ConsoleProfile as ConsoleProfile,
    LoginStep as LoginStep,
    PromptConsoleProfile as PromptConsoleProfile,
    RawConsoleProfile as RawConsoleProfile,
)
from ._stream import ConsoleStream as ConsoleStream, console_stream as console_stream

__all__ = [
    "ConsoleExecutor",
    "ConsoleProfile",
    "ConsoleStream",
    "LoginStep",
    "PosixConsoleProfile",
    "PromptConsoleProfile",
    "RawConsoleProfile",
    "console_stream",
]
