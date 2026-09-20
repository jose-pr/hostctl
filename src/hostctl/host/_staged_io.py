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
import typing
import warnings

Commit = typing.Callable[[bytes], None]


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
    ) -> None:
        super().__init__(value)
        self._commit = commit
        self._label = label

    def close(self) -> None:
        if not self.closed and self._commit is not None:
            value = self.getvalue()
            commit, self._commit = self._commit, None
            try:
                commit(value)
            finally:
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
            warnings.warn(
                f"unclosed {self._label} write stream discarded without committing",
                ResourceWarning,
                stacklevel=2,
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
    )
    if "a" in mode:
        stream.seek(0, io.SEEK_END)
    elif not readable:
        stream.seek(0)
    return stream
