"""``hostctl cp``: copy a file or tree between hosts."""

from __future__ import annotations

import contextlib

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
            source.copy(
                target,
                overwrite=self.overwrite,
                recursive=self.recursive,
            )
        return 0
