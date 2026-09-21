"""Staged write-back streams shared by the whole-file path backends.

WinRM, the Docker archive API, and the QEMU guest agent all expose a
read-whole-file / write-whole-file interface with no seekable remote handle,
so `open()` in a writable mode is emulated the same way everywhere: stage the
current contents in memory, let the caller work on them, and upload once on
`close()`.

This module exists because that emulation was written out three times.  The
copies were byte-identical apart from one thing -- only the WinRM one had the
`__del__` guard -- and that difference was the bug: `io.IOBase.__del__` calls
`close()`, and `close()` is what uploads, so an abandoned container or QGA
write stream performed its transfer at an arbitrary garbage-collection point
with any transport error swallowed by the interpreter.
"""

from __future__ import annotations

import io
import stat
import typing
import warnings

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


class StagedWriteStream(io.BytesIO):
    """An in-memory file whose contents are uploaded once, on `close()`.

    `label` names the transport in the abandonment warning; it is the only
    thing the three backends need to differ on.
    """

    def __init__(
        self,
        value: bytes,
        commit: typing.Optional[Commit],
        *,
        label: str,
        truncated: bool = False,
    ) -> None:
        super().__init__(value)
        self._commit = commit
        self._label = label
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

    def __exit__(self, *exc_info) -> None:
        # A `with` block that raised must not replace the destination. Without
        # this, `IOBase.__exit__` closed the stream and the commit ran anyway,
        # so an interrupted copy uploaded whatever had been staged -- usually
        # b'', because a failing read raises rather than returning a partial
        # buffer. docs/guide/transfer.md promises the opposite.
        if exc_info and exc_info[0] is not None and self._commit is not None:
            warnings.warn(
                f"{self._label} write stream discarded without committing: "
                f"{exc_info[0].__name__} left the block",
                ResourceWarning,
                stacklevel=2,
            )
            self.discard()
        self.close()

    def __del__(self):
        # Never upload from the collector. This guards the BINARY case only:
        # `pathlib_next.Path.open()` wraps a text mode in its own
        # `io.TextIOWrapper`, and that wrapper's finalizer closes the buffer,
        # which commits -- so an abandoned or exception-interrupted text
        # stream still uploads. Closing that needs a wrapper hook upstream;
        # see ../pathlib-next findings. `io.IOBase.__del__` calls
        # `close()`, so without this the pending commit would run at an
        # arbitrary point with its exceptions printed and discarded -- a
        # network write nobody asked for and nobody can catch.
        if getattr(self, "_commit", None) is not None and not self.closed:
            # No `stacklevel`: this runs during finalization, where the
            # stack is the collector's, not the opener's. Pointing at it was
            # worse than pointing at nothing.
            warnings.warn(
                f"unclosed {self._label} write stream discarded without committing",
                ResourceWarning,
            )
            self._commit = None
        try:
            super().close()
        except Exception:
            pass


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


class StagedTextWrapper(io.TextIOWrapper):
    """A text view whose finalizer does not let the staged buffer commit.

    `TextIOWrapper.__exit__` and its finalizer both call `close()`, which
    closes the buffer -- and for a staged stream that *is* the upload. The
    buffer's own guards never see an unclosed stream, so the discard has to
    happen here, before the close reaches it.
    """

    def _discard_pending(self, reason: str) -> None:
        try:
            buffer = self.buffer
            pending = getattr(buffer, "_commit", None) is not None
        except Exception:  # pragma: no cover - finalization races
            return
        if not pending:
            return
        warnings.warn(
            f"{getattr(buffer, '_label', 'staged')} write stream discarded "
            f"without committing: {reason}",
            ResourceWarning,
            stacklevel=3,
        )
        typing.cast(StagedWriteStream, buffer).discard()

    def __exit__(self, *exc_info) -> None:
        if exc_info and exc_info[0] is not None:
            self._discard_pending(f"{exc_info[0].__name__} left the block")
        self.close()

    def __del__(self) -> None:
        try:
            closed = self.closed
        except Exception:  # pragma: no cover - finalization races
            closed = True
        if not closed:
            self._discard_pending("never closed")
        try:
            super().__del__()  # type: ignore[misc]
        except AttributeError:  # pragma: no cover - build without IOBase.__del__
            try:
                self.close()
            except Exception:
                pass


def _text_encoding(encoding: typing.Optional[str]) -> typing.Optional[str]:
    # `io.text_encoding` is 3.10+; on the 3.9 floor pass the value through,
    # exactly as pathlib_next's own open() does.
    resolve = getattr(io, "text_encoding", None)
    return resolve(encoding) if resolve is not None else encoding


class StagedOpenMixin:
    """Supplies `open()` for a path whose `_open()` stages writes in memory.

    Identical to the inherited `pathlib_next.Path.open()` except for the text
    wrapper it builds: a plain `io.TextIOWrapper` commits the staged buffer
    from `__exit__` and from the garbage collector, so `open('w')` ignored the
    guards that `open('wb')` honours.
    """

    def open(  # type: ignore[override]
        self,
        mode: str = "r",
        buffering: int = -1,
        encoding: typing.Optional[str] = None,
        errors: typing.Optional[str] = None,
        newline: typing.Optional[str] = None,
    ) -> io.IOBase:
        if "b" in mode:
            return super().open(mode, buffering, encoding, errors, newline)  # type: ignore[misc]
        binary = super().open(mode.replace("t", "") + "b", buffering)  # type: ignore[misc]
        try:
            return StagedTextWrapper(
                binary,
                _text_encoding(encoding),
                errors,
                newline,
                line_buffering=buffering == 1,
            )
        except BaseException:
            # An unknown encoding or newline fails after the handle is open;
            # leaving it would commit an empty upload at collection time.
            if isinstance(binary, StagedWriteStream):
                binary.discard()
            binary.close()
            raise


def validate_open_mode(mode: str) -> typing.Tuple[bool, bool]:
    """Validate a binary `open()` mode and return `(readable, writable)`.

    A `t` suffix is accepted and ignored, as `pathlib_next` allows: these
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


def staged_open(backend: object, path: str, mode: str, *, label: str) -> io.IOBase:
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
        label=label,
        # `w` and `x` have already replaced the file's contents by opening it,
        # so an empty write is a real change; `r+` and `a` are not dirty until
        # the caller writes something.
        truncated="w" in mode or "x" in mode,
    )
    if "a" in mode:
        stream.seek(0, io.SEEK_END)
    return stream
