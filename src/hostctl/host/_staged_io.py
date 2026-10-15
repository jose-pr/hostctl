"""Staged write-back streams shared by the whole-file path backends.

WinRM, the Docker archive API, and the QEMU guest agent all expose a
read-whole-file / write-whole-file interface with no seekable remote handle,
so `open()` in a writable mode is emulated the same way everywhere: stage the
current contents in memory, let the caller work on them, and upload once on
`close()`.

One implementation for all three, because `close()` is what uploads: every
copy must keep a failed `with` block and the garbage collector from closing
it into a transfer, and pathapi's `DiscardOnError` is that guard.
"""

from __future__ import annotations

import io
import stat
import typing

from pathapi.providers import DiscardOnError

Commit = typing.Callable[[bytes], None]


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
    preserve_metadata=...)` and expects it to OVERWRITE an existing file and
    to handle a directory source by recursing. The four copies of this body
    did neither: they raised `FileExistsError` unless given an `overwrite`
    keyword stdlib never passes, and opened a directory `"rb"`. So
    `pathlib.Path("report.csv").copy(ssh.path("/srv/report.csv"))` failed on
    an existing remote file while the same call onto a local path replaced
    it.

    `preserve_metadata` is best-effort: these backends carry a mode at most,
    and a backend without `chmod` says so rather than failing the copy.
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
        # A directory is copied as a directory, as stdlib does.
        destination.mkdir(exist_ok=True)
        for child in source.iterdir():
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
        try:
            mode = source.stat().st_mode
        except (OSError, NotImplementedError):
            mode = None
        if mode:
            try:
                destination.chmod(stat.S_IMODE(mode))
            except (OSError, NotImplementedError):
                pass
    return destination


class StagedWriteStream(DiscardOnError, io.BytesIO):
    """An in-memory file whose contents are uploaded once, on `close()`.

    `DiscardOnError` keeps a failed `with` block, and a stream collected
    unclosed, from uploading; pathapi's `open()` gives a text mode the same
    guard because the stream has `discard()`.
    """

    def __init__(
        self,
        value: bytes,
        commit: typing.Optional[Commit],
        *,
        truncated: bool = False,
    ) -> None:
        super().__init__(value)
        self._commit = commit
        # A mode that already replaced the file (`w`, `x`) is dirty from the
        # start; `r+`/`a` are not until something writes.
        self._dirty = truncated

    def write(self, data) -> int:  # type: ignore[override]
        self._dirty = True
        return super().write(data)

    def writelines(self, lines) -> None:  # type: ignore[override]
        self._dirty = True
        super().writelines(lines)

    def truncate(self, size=None) -> int:  # type: ignore[override]
        self._dirty = True
        return super().truncate(size)

    def close(self) -> None:
        if not self.closed and self._commit is not None:
            if not self._dirty:
                # Nothing was written. `open("r+")` or `open("a")` used to
                # re-upload the whole file on close -- a full transfer, a new
                # mtime, and a clobber of whatever changed remotely in the
                # meantime -- for a caller that only read a header.
                self._commit = None
                super().close()
                return
            value = self.getvalue()
            commit = self._commit
            try:
                commit(value)
            except BaseException:
                # Keep the buffer AND the callback: the staged bytes are the
                # only copy, and closing here made them unrecoverable while a
                # retried close() returned silently having written nothing.
                raise
            self._commit = None
            super().close()
        else:
            super().close()

    def discard(self) -> None:
        """Drop the pending upload; a later `close()` writes nothing."""
        self._commit = None

    def _holds_uncommitted(self) -> bool:
        return self._commit is not None and self._dirty


class StagedAppendStream(StagedWriteStream):
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
    backends stage bytes and the text layer is applied above them. The
    container and QGA copies used to reject `"rt"` while the WinRM copy
    accepted it, for no reason either could have defended.
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
    staged read.
    """
    readable, writable = validate_open_mode(mode)
    open_read = getattr(backend, "open_read", None)
    if "r" in mode and not writable and open_read is not None:
        return open_read(path)
    if "x" in mode:
        # Exclusive create, checked NOW. Deferring it to the commit let the
        # caller do all its work against a stream that was never going to be
        # written.
        exists = getattr(backend, "exists", None)
        present = None
        if callable(exists):
            try:
                present = bool(exists(path))
            except NotImplementedError:
                present = None
        if present is None:
            try:
                typing.cast(typing.Any, backend).read_bytes(path)
            except FileNotFoundError:
                present = False
            except OSError:
                present = True
            else:
                present = True
        if present:
            raise FileExistsError(path)
    if "r" in mode or "a" in mode:
        try:
            value = typing.cast(typing.Any, backend).read_bytes(path)
        except FileNotFoundError:
            # `a` creates what is missing; `r`/`r+` must still fail.
            if "a" not in mode:
                raise
            value = b""
    else:
        value = b""
    stream_type = StagedAppendStream if "a" in mode else StagedWriteStream
    stream = stream_type(
        value,
        (
            (
                lambda data: typing.cast(typing.Any, backend).write_bytes(
                    path, data, exclusive="x" in mode
                )
            )
            if writable
            else None
        ),
        # `w` and `x` have already replaced the file's contents by opening it,
        # so an empty write is a real change; `r+` and `a` are not dirty until
        # the caller writes something.
        truncated="w" in mode or "x" in mode,
    )
    if "a" in mode:
        stream.seek(0, io.SEEK_END)
    return stream
