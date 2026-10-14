"""``hostctl cp``: copy a file or tree between hosts."""

from __future__ import annotations

import contextlib

from pathlib_next import Path as NextPath

from ._common import Credentialed


class Cp(Credentialed):
    """Copy a file or tree; either operand may be URI:PATH."""

    _parsername_ = "cp"

    overwrite: bool = False
    "Replace an existing target"

    recursive: bool = False
    "Copy a whole tree"

    source: str
    "URI:PATH, or a local filesystem path"
    ("source",)

    target: str
    "URI:PATH, or a local filesystem path"
    ("target",)

    def __call__(self) -> int:
        with contextlib.ExitStack() as stack:
            source = self.path_operand(stack, self.source)
            target = self.path_operand(stack, self.target)
            # Python 3.14's stdlib pathlib.Path.copy() wins LocalPath's mixed
            # MRO, so route through pathlib_next to keep one copy contract.
            NextPath.copy(
                source,
                target,
                overwrite=self.overwrite,
                recursive=self.recursive,
            )
        return 0
