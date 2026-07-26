"""Persistent process contracts and provider adapters."""

from ._common import (
    Process as Process,
    ProcessData as ProcessData,
    TerminalOptions as TerminalOptions,
    TerminalRequest as TerminalRequest,
    terminal_options as terminal_options,
)
from .ssh import SshProcess as SshProcess
from .container import ContainerProcess as ContainerProcess
from .serial import (
    SerialConsoleProcess as SerialConsoleProcess,
    SerialProcess as SerialProcess,
)
from .qemu_serial import (
    ConsoleResize as ConsoleResize,
    ConsoleStreamFactory as ConsoleStreamFactory,
    QemuConsoleStream as QemuConsoleStream,
    QemuSerialConsole as QemuSerialConsole,
    QemuSerialProcess as QemuSerialProcess,
    normalize_qemu_console_error as normalize_qemu_console_error,
)

__all__ = (
    "Process",
    "ProcessData",
    "ConsoleResize",
    "ConsoleStreamFactory",
    "QemuConsoleStream",
    "QemuSerialConsole",
    "QemuSerialProcess",
    "ContainerProcess",
    "SshProcess",
    "SerialProcess",
    "SerialConsoleProcess",
    "TerminalOptions",
    "TerminalRequest",
    "terminal_options",
    "normalize_qemu_console_error",
)
