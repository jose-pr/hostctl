"""Optional PowerShell Remoting Protocol execution provider."""

from __future__ import annotations

import importlib
import subprocess
import typing

from ._common import (
    CaptureOutput,
    CommandArgument,
    Executor,
    ExecutorCapability,
    ExecutorCommand,
    FileHandle,
    Input,
    capture_streams,
    dispatch_output,
)


def pypsrp_available() -> bool:
    """Return whether the optional PSRP dependency can be imported."""
    if __import__("sys").version_info < (3, 10):
        return False
    try:
        importlib.import_module("pypsrp")
    except ImportError:
        return False
    return True


def require_pypsrp() -> typing.Any:
    """Import pypsrp with an actionable, version-aware error."""
    import sys

    if sys.version_info < (3, 10):
        raise ImportError(
            "PSRP support requires Python 3.10 or newer; use the pywinrm "
            "provider on Python 3.9"
        )
    try:
        return importlib.import_module("pypsrp")
    except ImportError as exc:
        raise ImportError(
            "PSRP support requires the 'psrp' extra: " "pip install hostctl[psrp]"
        ) from exc


class PsrpExecutor(Executor[subprocess.CompletedProcess]):
    """Execute finalized PowerShell scripts in fresh PSRP pipelines."""

    # PSRP receives a finalized PowerShell script; cwd/env are embedded by
    # the host shell rather than forwarded as unsupported executor options.
    executor_capabilities = frozenset(
        (ExecutorCapability.SCRIPT, ExecutorCapability.MANAGES_STATUS)
    )

    def __init__(self, session: typing.Callable[[], typing.Any]) -> None:
        self._session = session

    def __call__(
        self,
        command: ExecutorCommand,
        *args: CommandArgument,
        bufsize: int = -1,
        stdin: typing.Optional[FileHandle] = None,
        stdout: typing.Optional[FileHandle] = None,
        stderr: typing.Optional[FileHandle] = None,
        capture_output: CaptureOutput = True,
        check: bool = True,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
        input: Input = None,
        timeout: typing.Optional[float] = None,
        text: typing.Optional[bool] = None,
        **options: object,
    ) -> subprocess.CompletedProcess:
        del bufsize
        if options:
            raise TypeError(f"unsupported PSRP executor option: {sorted(options)[0]}")
        if args:
            raise NotImplementedError("PSRP executor receives a finalized script")
        if stdin is not None or input is not None:
            raise NotImplementedError("PSRP does not provide raw stdin for run()")
        if timeout is not None:
            raise NotImplementedError(
                "PSRP pipeline timeout is unsupported; configure WinRM timeouts"
            )
        stdout, stderr = capture_streams(capture_output, stdout, stderr)
        result = self._session().invoke(str(command), raw=False, capture_exit=True)
        codec = encoding or "utf-8"
        # Preserve PowerShell's object-pipeline line orientation when
        # projecting objects to subprocess-compatible text.
        projected_output = []
        returncode = result.returncode
        marker = "__HOSTCTL_LASTEXITCODE__:"
        for item in result.output:
            value = str(item)
            if value.startswith(marker):
                try:
                    parsed_returncode = int(value[len(marker) :])
                    returncode = (
                        1
                        if result.had_errors and parsed_returncode == 0
                        else parsed_returncode
                    )
                except ValueError:
                    returncode = 1
                continue
            projected_output.append(item)
        out_text = "\n".join(str(item) for item in projected_output)
        err_text = "\n".join(str(item) for item in result.streams.error)
        if out_text:
            out_text += "\n"
        out: typing.Union[str, bytes] = (
            out_text if text or encoding else out_text.encode(codec)
        )
        err: typing.Union[str, bytes] = (
            err_text if text or encoding else err_text.encode(codec)
        )
        if stderr is subprocess.STDOUT:
            out = out + err
            err = None
        out, err = dispatch_output(
            stdout,
            stderr,
            out,
            err,
            encoding=encoding,
            errors=errors,
        )
        completed = subprocess.CompletedProcess(
            args=str(command),
            returncode=returncode,
            stdout=out,
            stderr=err,
        )
        if check:
            completed.check_returncode()
        return completed
