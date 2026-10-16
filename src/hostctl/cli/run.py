"""``hostctl run``: one program on the host, without a shell."""

from __future__ import annotations

import sys

from duho import CommandError

from ..shell._grammar import Exec
from ._common import FAILED, HostCommand, write


class Run(HostCommand):
    """Run one command directly, without a shell: hostctl run URI -- PROGRAM [ARG...].

    Exits with the remote command's own status.
    """

    _parsername_ = "run"
    _examples_ = ["hostctl run local: -- python -c \"print('hello')\""]

    def __call__(self) -> int:
        command = list(self._passthrough_)
        if not command:
            raise CommandError("run requires a command after --", FAILED)
        with self.connect() as host:
            # `Exec` takes the program verbatim, so a bare name resolves
            # through the target's PATH and an absolute path is used as given.
            result = host.run(
                Exec(command[0], *command[1:]),
                check=False,
                capture_output=True,
            )
        write(sys.stdout, result.stdout)
        write(sys.stderr, result.stderr)
        return int(result.returncode)
