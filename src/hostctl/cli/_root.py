"""The root parser: six subcommands under one program name."""

from __future__ import annotations

import argparse
import subprocess
import sys

from duho import AUTO, Cli

from ..exceptions import OperationNotStartedError
from .cat import Cat
from .cp import Cp
from .info import Info
from .ls import Ls
from .run import Run
from .shell import Shell

_EPILOG = """\
connection URIs:
  local:                      this machine
  ssh://user@host:22          SSH (asyncssh)
  winrm://user@host:5985      WinRM
  winrm://user@host:5985?provider=psrp  PowerShell Remoting (PSRP)
  docker://container          a running container
  qemu+libvirt:///domain      a guest, through libvirt
  serial:///dev/ttyUSB0       a raw serial console

remote path operands (cp):
  URI:PATH -- the colon after the URI separates it from the path.
  local:/tmp/x   ssh://host:/srv/app   ssh://host:2222:/srv/app
  A bare filesystem path (./x, C:\\x) is this machine.

credentials:
  A password comes from the HOSTCTL_PASSWORD environment variable, or from
  --ask-password, and never from the command line, where it would reach the
  process table and the shell history. It is offered only to schemes that
  accept one.

exit status:
  0    the command succeeded (for `run`, the remote command's own status)
  125  connection, timeout, usage, or an unsupported operation
  126  permission denied
  127  not found
  130  interrupted
"""

#: The first `isinstance` match wins, so both OSError subclasses come before
#: OSError itself.
_ERRORS = {
    subprocess.TimeoutExpired: 125,
    PermissionError: 126,
    FileNotFoundError: 127,
    # Raised only before anything was sent: the host was never reached.
    OperationNotStartedError: 125,
    ConnectionError: 125,
    OSError: 125,
    ValueError: 125,
    NotImplementedError: 125,
}

_PREFIXES = (
    (subprocess.TimeoutExpired, "timed out: "),
    (PermissionError, "permission denied: "),
    (FileNotFoundError, "not found: "),
)


def _report(message: str, code: int, error: BaseException) -> None:
    """One `hostctl: ...` line on stderr, naming the kind of failure."""
    prefix = next((text for kind, text in _PREFIXES if isinstance(error, kind)), "")
    print(f"hostctl: {prefix}{message}", file=sys.stderr)


class Hostctl(Cli):
    """Run commands and move files on a host named by a URI."""

    _parsername_ = "hostctl"
    _version_ = AUTO
    _distribution_ = "hostctl"
    _subcommands_ = [Run, Ls, Cat, Cp, Info, Shell]
    _help_formatter_ = argparse.RawDescriptionHelpFormatter
    _errors_ = _ERRORS
    _report_error_ = staticmethod(_report)
    _exit_codes_ = {
        125: "connection, timeout, usage, or an unsupported operation",
        126: "permission denied",
        127: "not found",
        130: "interrupted",
    }

    @classmethod
    def _initparser_(cls, parser, **kwargs):
        parser = super()._initparser_(parser, **kwargs)
        parser.epilog = _EPILOG
        return parser
