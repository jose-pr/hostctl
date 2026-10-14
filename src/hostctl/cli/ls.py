"""``hostctl ls``: the names in a directory on the host."""

from __future__ import annotations

import sys

from ._common import HostCommand, write


class Ls(HostCommand):
    """List a directory on the host, one name per line."""

    _parsername_ = "ls"

    path: str
    "A path on the host, not a URI:PATH operand"
    ("path",)

    def __call__(self) -> int:
        with self.connect() as host:
            for child in host.path(self.path).iterdir():
                write(sys.stdout, f"{child.name}\n")
        return 0
