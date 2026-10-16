"""Staged write-back streams shared by the whole-file path backends.

WinRM, the Docker archive API, and the QEMU guest agent all expose a
read-whole-file / write-whole-file interface with no seekable remote handle,
so `open()` in a writable mode is emulated the same way everywhere: stage the
current contents in memory, let the caller work on them, and upload once on
`close()`.

One implementation for all three, because `close()` is what uploads: every
copy must keep a failed `with` block and the garbage collector from closing
it into a transfer, and pathapi's `CommitOnClose` is that guard.
"""

from __future__ import annotations

import io
import stat
import typing

from pathapi.providers import CommitOnClose, is_safe_child_name, is_windows_flavoured

from ..exceptions import OperationNotStartedError


def copy_from(
    destination,
    source,
    *,
    follow_symlinks: bool = True,
    preserve_metadata: bool = False,
    overwrite: bool = True,
    **_unused: object,
):
    """CPython 3.14's `Path.copy()` destination hook, for one whole-file path.

    Stdlib calls `destination._copy_from(source, follow_symlinks=...,
    preserve_metadata=...)`: a file replaces an existing one, a directory
    source is recreated and recursed into, and an existing destination
    directory is `FileExistsError`. pathapi's own hook checks no child name,
    so a source entry whose name holds a separator would land outside the destination; this
    one refuses any name that is not a single component.

    `preserve_metadata` is best-effort: these backends carry a mode at most,
    and a backend without `chmod` or `_utime` is skipped, not failed.
    """
    if not follow_symlinks and getattr(source, "is_symlink", lambda: False)():
        link = getattr(source, "readlink", None)
        symlink_to = getattr(destination, "symlink_to", None)
        if callable(link) and callable(symlink_to):
            symlink_to(str(link()))
            return destination
        raise NotImplementedError(
            "copy(follow_symlinks=False) is unsupported by this backend"
        )
    if getattr(source, "is_dir", lambda: False)():
        destination.mkdir()
        windows = is_windows_flavoured(destination)
        for child in source.iterdir():
            if not is_safe_child_name(child.name, windows=windows):
                raise ValueError(
                    f"refusing unsafe child name {child.name!r} under {destination}"
                )
            copy_from(
                destination / child.name,
                child,
                follow_symlinks=follow_symlinks,
                preserve_metadata=preserve_metadata,
                overwrite=overwrite,
            )
        return destination
    if not overwrite and destination.exists():
        raise FileExistsError(str(destination))
    with source.open("rb") as reader, destination.open("wb") as writer:
        while chunk := reader.read(1024 * 1024):
            writer.write(chunk)
    if preserve_metadata:
        # `OperationNotStartedError` is an OSError; a composite path whose
        # providers all declined must say so, not skip the metadata.
        try:
            info = source.stat()
        except OperationNotStartedError:
            raise
        except (OSError, NotImplementedError):
            return destination
        mode = getattr(info, "st_mode", None)
        if mode:
            try:
                destination.chmod(stat.S_IMODE(mode))
            except OperationNotStartedError:
                raise
            except (OSError, NotImplementedError):
                pass
        mtime = getattr(info, "st_mtime", None)
        if mtime is not None:
            try:
                destination._utime(mtime=mtime)
            except OperationNotStartedError:
                raise
            except (OSError, NotImplementedError):
                pass
    return destination


class StagedAppendStream(CommitOnClose):
    """A staged stream whose writes always land at the end, as `O_APPEND` does.

    Seeking to the end once at open is not enough: the stream is a seekable
    buffer, so any `seek()` or `read()` moved the position and the next
    `write()` overwrote existing bytes -- silently, and only visible once the
    whole buffer was uploaded on close. Real `a`/`a+` files append regardless
    of position.
    """

    def write(self, data) -> int:  # type: ignore[override]
        self.seek(0, io.SEEK_END)
        return super().write(data)

    def writelines(self, lines) -> None:  # type: ignore[override]
        self.seek(0, io.SEEK_END)
        super().writelines(lines)


def validate_open_mode(mode: str) -> typing.Tuple[bool, bool]:
    """Validate a binary `open()` mode and return `(readable, writable)`.

    A `t` suffix is accepted and ignored, as `pathapi` allows: these
    backends stage bytes and the text layer is applied above them.
    """
    if (
        not mode
        or sum(mode.count(value) for value in "rwax") != 1
        or mode.count("+") > 1
        or mode.count("t") > 1
        or len(mode.replace("t", "")) != 1 + mode.count("+")
    ):
        raise ValueError(f"invalid mode: {mode!r}")
    readable = "r" in mode or "+" in mode
    writable = any(value in mode for value in "wax+")
    return readable, writable


def staged_open(backend: object, path: str, mode: str) -> io.IOBase:
    """Open `path` on a whole-file backend, staging writes in memory.

    A read-only mode is delegated to the backend's own `open_read()` when it
    has one, so a backend that streams ranges is not forced through a full
    staged read. A writable mode returns pathapi's `CommitOnClose`: the
    backend's `write_bytes` runs once, on `close()`, and not at all after a
    failed `with` block or an unwritten `r+`/`a`.
    """
    readable, writable = validate_open_mode(mode)
    target = typing.cast(typing.Any, backend)
    if "r" in mode and not writable:
        open_read = getattr(backend, "open_read", None)
        if open_read is not None:
            return open_read(path)
        return io.BufferedReader(io.BytesIO(target.read_bytes(path)))
    if "x" in mode:
        # Exclusive create, checked NOW. Deferring it to the commit let the
        # caller do all its work against a stream that was never going to be
        # written. Only the backend's own answer counts: a server error says
        # nothing about the path and propagates.
        exists = getattr(backend, "exists", None)
        if not callable(exists):
            raise NotImplementedError("exclusive open needs the backend's exists()")
        if exists(path):
            raise FileExistsError(path)
    exclusive = "x" in mode

    def commit(buffer: io.BytesIO) -> None:
        target.write_bytes(path, buffer.getvalue(), exclusive=exclusive)

    if "w" in mode or "x" in mode:
        # The file is replaced by opening it, so an empty write is a change.
        return CommitOnClose(commit)
    try:
        value = target.read_bytes(path)
    except FileNotFoundError:
        # `a` creates what is missing; `r+` must still fail.
        if "a" not in mode:
            raise
        value = b""
    if "a" in mode:
        stream = StagedAppendStream(commit, value)
        stream.seek(0, io.SEEK_END)
        return stream
    return CommitOnClose(commit, value)
