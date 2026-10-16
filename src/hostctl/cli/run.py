"""``hostctl run``: one program on the host, or on several, without a shell."""

from __future__ import annotations

import subprocess
import sys
import typing

from duho import CommandError

from ..shell._grammar import Exec
from ._common import FAILED, write
from ._fanout import FanOutCommand, prefixed


class Run(FanOutCommand):
    """Run one command directly, without a shell: hostctl run URI [URI...] -- PROGRAM [ARG...].

    Exits with the remote command's own status. On several hosts, each output
    line is prefixed with its host and the status is the worst of theirs.
    """

    _parsername_ = "run"
    _examples_ = [
        "hostctl run local: -- python -c \"print('hello')\"",
        "hostctl run 'ssh://admin@nas[1-3]' --parallel 3 -- uptime",
    ]

    def __call__(self) -> int:
        command = list(self._passthrough_)
        if not command:
            raise CommandError("run requires a command after --", FAILED)
        targets = self.targets()
        if len(targets) > 1:
            return self.fan_out(targets, lambda host: _run(host, command), _emit)
        with self.open(targets[0], self.credentials()) as host:
            result = _run(host, command)
        write(sys.stdout, result.stdout)
        write(sys.stderr, result.stderr)
        return int(result.returncode)


def _run(host: typing.Any, command: typing.List[str]) -> subprocess.CompletedProcess:
    # `Exec` takes the program verbatim, so a bare name resolves through the
    # target's PATH and an absolute path is used as given.
    return host.run(Exec(command[0], *command[1:]), check=False, capture_output=True)


def _emit(label: str, result: subprocess.CompletedProcess, out, err) -> int:
    write(out, prefixed(label, result.stdout))
    write(err, prefixed(label, result.stderr))
    return int(result.returncode)


__all__ = [
    "Run",
]
