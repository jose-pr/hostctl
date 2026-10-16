"""Guest path classes backed by the guest agent."""

from __future__ import annotations


import typing


from pathapi import WindowsPathname
from pathapi.protocols import Chmod, Create, Listing, Move, Remove, Stat, Utime
from pathapi.providers import notimplemented


from ...paths._backend import PosixBackendPath, WindowsBackendPath
from ._backend import QGAPathBackend

#: The guest-helper operations each protocol's primitives need. QGA's file
#: RPCs move bytes and nothing else, so these hold only with a helper.
_HELPER_OPERATIONS = {
    Stat: ("stat",),
    Listing: ("scandir",),
    Create: ("mkdir",),
    Remove: ("unlink", "rmdir"),
    Chmod: ("chmod",),
    Move: ("rename",),
    Utime: ("utime",),
}


class _QGAPathMixin:
    __slots__ = ()

    def _supports(self, protocol) -> typing.Optional[bool]:
        """Answer from the helper probed at discovery; no request is made."""
        operations = _HELPER_OPERATIONS.get(protocol)
        if operations is None:
            return None
        helper = self.backend.helper
        return helper is not None and all(
            callable(getattr(helper, name, None)) for name in operations
        )

    def exists(self, *, follow_symlinks: bool = True) -> bool:
        # pathapi answers this from `stat()`, swallowing OSError but not
        # NotImplementedError -- so on a helper-less guest the documented
        # boolean probe raised. The backend can still answer with file RPCs.
        return self.backend.exists(str(self), follow_symlinks=follow_symlinks)

    def _check_open(self, mode: str) -> None:
        if "x" in mode and self.backend.helper is None:
            # `staged_open` would return a writable stream and only call
            # `write_bytes` from `close()`, so the refusal arrived after the
            # caller had streamed the whole payload into memory -- from the
            # context manager's exit, where nothing was wrapped in a
            # capability check. Every other unsupported QGA operation is
            # refused before any work happens.
            raise NotImplementedError(
                "exclusive QGA writes require a transactional guest helper"
            )

    @notimplemented(
        "QEMU Guest Agent has no symlink RPC; the guest-file-* commands "
        "only open, read, write, seek, flush, and close regular files"
    )
    def _symlink_to(self, target, target_is_directory: bool = False):
        """Always raise -- QGA exposes no symlink RPC.

        The guest agent's file protocol is limited to opening, reading,
        writing, seeking, flushing, and closing handles.  Emulating a
        symlink through ``guest-exec`` would be a different transport with
        different failure and permission semantics, so the gap is reported
        explicitly instead of being faked.
        """

    @notimplemented(
        "QEMU Guest Agent has no readlink RPC; the guest-file-* commands "
        "only open, read, write, seek, flush, and close regular files"
    )
    def readlink(self): ...

    def _mkdir(self, mode: int):
        self.backend.mkdir(str(self), mode)

    def _rename_on_backend(self, target) -> None:
        # `os.rename`'s own rule per family: a POSIX guest replaces, a Windows
        # guest refuses an existing target.
        self.backend.rename(
            str(self), str(target), replace=not isinstance(self, WindowsPathname)
        )


class PosixQEMUPath(_QGAPathMixin, PosixBackendPath):
    """A POSIX guest path backed by QEMU Guest Agent."""

    __slots__ = ()
    _backend_type = QGAPathBackend


class WindowsQEMUPath(_QGAPathMixin, WindowsBackendPath):
    """A Windows guest path backed by QEMU Guest Agent."""

    __slots__ = ()
    _backend_type = QGAPathBackend
