"""``hostctl deploy``: install a Python program on the host, then run it."""

from __future__ import annotations

import sys
import typing

from duho import Arg, Meta

from ..shell._grammar import Exec
from ._common import HostCommand, write


class Deploy(HostCommand):
    """Install a Python program on the host: hostctl deploy URI SOURCE PATH [-- ARG...].

    SOURCE is a distribution installed here; only what the host's python
    lacks is shipped. Arguments after -- run the installed program with them,
    and the exit status is then its own.
    """

    _parsername_ = "deploy"
    _examples_ = [
        "hostctl deploy ssh://admin@nas mytool /opt/mytool.pyz",
        "hostctl deploy ssh://admin@nas mytool /opt/mytool --mode dir -- --version",
        "hostctl deploy ssh://nas . /opt/mytool --tree --mode dir --package mytool",
    ]

    source: str
    "Distribution installed here to ship with what it needs; with --tree, a directory"
    ("source",)

    path: str
    "Absolute path on the host for the zipapp file or the directory"
    ("path",)

    mode: Arg[str, Meta(choices=("pyz", "dir"))] = "pyz"
    "pyz: one executable zipapp file; dir: lib/ and a bin/PACKAGE launcher"

    package: str = ""
    "Import name to run (default: SOURCE with - as _; required with --tree)"

    extra: Arg[typing.List[str], Meta(metavar="EXTRA")] = []
    "Include this extra of SOURCE (repeatable)"

    skip: Arg[typing.List[str], Meta(metavar="DIST")] = []
    "Never ship this distribution, even when the host lacks it (repeatable)"

    force: bool = False
    "Write even when the host already holds this exact bundle"

    python: str = "python3"
    "The host's Python interpreter, a name on its PATH or an absolute path"

    tree: bool = False
    "SOURCE is a working tree to ship as it is, filtered by its ignore files (dir only)"

    ignore_file: Arg[typing.List[str], Meta(metavar="FILE")] = []
    "With --tree, read this ignore file instead of .gitignore, .ignore and .bundleignore (repeatable)"

    ignore_pattern: Arg[typing.List[str], Meta(metavar="PATTERN")] = []
    "With --tree, also ignore this gitignore pattern; !PATTERN re-includes (repeatable)"

    pythonpath: Arg[typing.List[str], Meta(metavar="DIR")] = []
    "With --tree, a directory of the tree holding the package (repeatable; default: detected)"

    def __call__(self) -> int:
        from ..deploy import deploy

        source = self._source()
        with self.connect() as host:
            done = deploy(
                host,
                source,
                self.path,
                mode=self.mode,
                package=self.package or None,
                extras=self.extra,
                skip=self.skip,
                force=self.force,
                python=self.python,
            )
            arguments = list(self._passthrough_)
            if not arguments:
                state = "deployed" if done.written else "current"
                write(sys.stdout, f"{state} {done.path} {done.digest[:12]}\n")
                return 0
            result = host.run(
                Exec(*done.command, *arguments), check=False, capture_output=True
            )
        write(sys.stdout, result.stdout)
        write(sys.stderr, result.stderr)
        return int(result.returncode)

    def _source(self) -> typing.Any:
        """The distribution name, or the tree the options describe."""
        if not self.tree:
            return self.source
        from ..deploy import SourceTree

        return SourceTree(
            self.source,
            ignore_files=self.ignore_file or None,
            ignore_patterns=self.ignore_pattern,
            pythonpath=self.pythonpath or None,
        )


__all__ = [
    "Deploy",
]
