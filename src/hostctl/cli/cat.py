"""``hostctl cat``: a file's bytes on standard output."""

from __future__ import annotations

import sys

from ._common import HostCommand, write


class Cat(HostCommand):
    """Write a file's bytes to stdout."""

    _parsername_ = "cat"

    path: str
    "A path on the host, not a URI:PATH operand"
    ("path",)

    def __call__(self) -> int:
        with self.connect() as host:
            write(sys.stdout, host.path(self.path).read_bytes())
        return 0


__all__ = [
    "Cat",
]
