"""The composite POSIX and Windows path classes."""

from __future__ import annotations


from pathapi import Path, PosixPathname, WindowsPathname


from ._mixin import _CompositePathMixin


class CompositePosixPath(_CompositePathMixin, PosixPathname, Path):
    """A provider-selecting path with POSIX syntax on every client OS."""

    __slots__ = (
        "_provider",
        "_backend_path",
        "_providers",
        "_selector",
        "_factory",
        "_pinned",
        "_selection_trace",
    )


class CompositeWindowsPath(_CompositePathMixin, WindowsPathname, Path):
    """A provider-selecting path with Windows syntax on every client OS."""

    __slots__ = (
        "_provider",
        "_backend_path",
        "_providers",
        "_selector",
        "_factory",
        "_pinned",
        "_selection_trace",
    )
