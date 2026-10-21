"""The root parser: seven subcommands under one program name."""

from __future__ import annotations

import argparse
import sys
import traceback

from duho import AUTO, Cli
from duho.logging import traceback_enabled

from ._status import ERRORS, EXIT_CODES, describe
from .cat import Cat
from .cp import Cp
from .deploy import Deploy
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

several hosts (run, info):
  hostctl run URI [URI...] -- PROGRAM; each URI may list hosts with commas
  and [1-3] or [a-c] ranges: ssh://admin@nas1,nas2  ssh://nas[1-3]
  A later host without a scheme takes the previous one's scheme and user.
  Each output line starts with [URI]; --parallel N runs N hosts at once.

credentials:
  -u USERNAME and -p PASSWORD replace the URI's user and password; -p '' is
  no password. Without -p the password is HOSTCTL_PASSWORD, else the URI's
  own, else a prompt on the terminal when a host asks for one. Without a
  terminal a host that needs a password fails (126).

exit status:
  0    the command succeeded (for `run`, the remote command's own status)
  125  connection, timeout, usage, a missing extra, or an unsupported operation
  126  permission denied
  127  not found
  130  interrupted
  On several hosts: the worst of theirs -- 130, then 125-127, then the
  largest remote status. Each failing host gets an "exit status" line.
"""


def _report(message: str, code: int, error: BaseException) -> None:
    """One `hostctl: ...` line on stderr, naming the kind of failure.

    duho leaves the traceback to the hook, so ``DUHO_TRACEBACK`` adds it.
    """
    print(f"hostctl: {describe(message, error)}", file=sys.stderr)
    if traceback_enabled():
        traceback.print_exception(
            type(error), error, error.__traceback__, file=sys.stderr
        )


class Hostctl(Cli):
    """Run commands and move files on a host named by a URI."""

    _parsername_ = "hostctl"
    _version_ = AUTO
    _distribution_ = "hostctl"
    _subcommands_ = [Run, Ls, Cat, Cp, Info, Shell, Deploy]
    _help_formatter_ = argparse.RawDescriptionHelpFormatter
    _errors_ = ERRORS
    _report_error_ = staticmethod(_report)
    _exit_codes_ = EXIT_CODES

    @classmethod
    def _initparser_(cls, parser, **kwargs):
        parser = super()._initparser_(parser, **kwargs)
        parser.epilog = _EPILOG
        return parser
