"""Public executor contracts."""

from ._common import (
    CaptureOutput as CaptureOutput,
    CommandArgument as CommandArgument,
    CommandLine as CommandLine,
    Environment as Environment,
    ExecutionOptions as ExecutionOptions,
    Executor as Executor,
    ExecutorCapability as ExecutorCapability,
    ExecutorCommand as ExecutorCommand,
    FileHandle as FileHandle,
    Input as Input,
    PathLike as PathLike,
    normalize_environment as normalize_environment,
    validate_environment_names as validate_environment_names,
    capture_streams as capture_streams,
    dispatch_output as dispatch_output,
    expired as expired,
    normalize_input as normalize_input,
    reject_stdin_conflict as reject_stdin_conflict,
    wants_text as wants_text,
    write_output as write_output,
)
from .container import (
    ContainerExecutor as ContainerExecutor,
    ContainerLike as ContainerLike,
    normalize_container_error as normalize_container_error,
)
from .ssh import SSHConnection as SSHConnection, SSHExecutor as SSHExecutor
from .local import LocalExecutor as LocalExecutor
from .serial import (
    SerialExecutor as SerialExecutor,
    SerialFactory as SerialFactory,
    SerialLike as SerialLike,
    SerialSettings as SerialSettings,
    normalize_serial_error as normalize_serial_error,
)
from .qemu import QEMUExecutor as QEMUExecutor
from .winrm import (
    NativeWinRMSession as NativeWinRMSession,
    WinRMExecutor as WinRMExecutor,
    WinRMSession as WinRMSession,
)
from .psrp import (
    PSRPExecutor as PSRPExecutor,
    pypsrp_available as pypsrp_available,
    require_pypsrp as require_pypsrp,
)

__all__ = [
    "CaptureOutput",
    "CommandLine",
    "ContainerExecutor",
    "ContainerLike",
    "CommandArgument",
    "Environment",
    "ExecutionOptions",
    "Executor",
    "ExecutorCapability",
    "ExecutorCommand",
    "FileHandle",
    "Input",
    "LocalExecutor",
    "PathLike",
    "QEMUExecutor",
    "NativeWinRMSession",
    "normalize_container_error",
    "normalize_environment",
    "validate_environment_names",
    "capture_streams",
    "dispatch_output",
    "expired",
    "normalize_input",
    "reject_stdin_conflict",
    "wants_text",
    "write_output",
    "normalize_serial_error",
    "SerialExecutor",
    "SerialFactory",
    "SerialLike",
    "SerialSettings",
    "SSHConnection",
    "SSHExecutor",
    "WinRMExecutor",
    "WinRMSession",
    "PSRPExecutor",
    "pypsrp_available",
    "require_pypsrp",
]
