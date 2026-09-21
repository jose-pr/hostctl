"""The staged write-back stream shared by the whole-file path backends.

WinRM, the Docker archive API, and the QGA file RPCs each emulate a writable
`open()` by staging bytes in memory and uploading on `close()`. That
emulation used to be written out three times, and only the WinRM copy had the
`__del__` guard -- so an abandoned container or QGA write stream performed a
network upload from the garbage collector, with any transport error printed
and swallowed by the interpreter.

These tests pin the guard for every backend, and the `t`-suffix question the
three copies used to answer differently.
"""

from __future__ import annotations

import gc
import io

import pytest

from hostctl.host._staged_io import StagedWriteStream, staged_open, validate_open_mode
from hostctl.host._winrm import WinRMPath
from hostctl.host.container_path import PosixContainerPath
from hostctl.host.qemu import PosixQemuPath


class _RecordingBackend:
    """The whole-file interface `staged_open` needs, and nothing else."""

    def __init__(self, contents=b"existing"):
        self.contents = contents
        self.writes = []

    def read_bytes(self, path):
        if self.contents is None:
            raise FileNotFoundError(path)
        return self.contents

    def write_bytes(self, path, value, *, exclusive=False):
        self.writes.append((path, value, exclusive))


def _paths():
    """One real path object per backend, all over the recording fake.

    The path classes only ever hand their backend to `staged_open`, so a
    duck-typed backend exercises the production `_open` of each flavour.
    """
    return {
        "winrm": lambda backend: WinRMPath(r"C:\data.bin", backend=backend),
        "container": lambda backend: PosixContainerPath("/data.bin", backend=backend),
        "qga": lambda backend: PosixQemuPath("/data.bin", backend=backend),
    }


@pytest.mark.parametrize("flavour", sorted(_paths()))
def test_abandoning_a_write_stream_warns_and_does_not_upload(flavour):
    backend = _RecordingBackend()
    path = _paths()[flavour](backend)

    stream = path.open("wb")
    stream.write(b"never sent")
    with pytest.warns(ResourceWarning, match="discarded without committing"):
        del stream
        gc.collect()

    assert backend.writes == [], f"{flavour} uploaded from the collector"


@pytest.mark.parametrize("flavour", sorted(_paths()))
def test_closing_a_write_stream_uploads_once(flavour):
    backend = _RecordingBackend()
    path = _paths()[flavour](backend)

    with path.open("wb") as stream:
        stream.write(b"payload")

    assert [value for _, value, _ in backend.writes] == [b"payload"]


@pytest.mark.parametrize("flavour", sorted(_paths()))
def test_text_suffix_is_accepted_by_every_backend(flavour):
    """The container and QGA copies used to reject `"rt"`; WinRM accepted it."""
    backend = _RecordingBackend(contents=b"value")
    path = _paths()[flavour](backend)

    with path.open("rt") as stream:
        assert stream.read() == "value"


def test_open_mode_validation_is_one_rule():
    # `pathlib_next` strips the `b` before calling `_open`, so these are the
    # modes the validator actually sees.
    assert validate_open_mode("r") == (True, False)
    assert validate_open_mode("w") == (False, True)
    assert validate_open_mode("r+") == (True, True)
    assert validate_open_mode("a") == (False, True)
    assert validate_open_mode("rt") == (True, False)
    for invalid in ("", "rw", "ra", "wx", "rr", "++", "r++", "rtt"):
        with pytest.raises(ValueError, match="invalid mode"):
            validate_open_mode(invalid)


def test_read_only_open_delegates_to_a_backend_that_streams():
    class _Streaming(_RecordingBackend):
        def open_read(self, path):
            return io.BytesIO(b"streamed")

    backend = _Streaming()
    stream = staged_open(backend, "/x", "r", label="test")
    assert not isinstance(stream, StagedWriteStream)
    assert stream.read() == b"streamed"

    # A backend without `open_read` still gets a staged read rather than an
    # AttributeError -- this is the branch only WinRM used to guard.
    plain = staged_open(_RecordingBackend(b"staged"), "/x", "r", label="test")
    assert plain.read() == b"staged"


def test_append_creates_a_missing_file_but_read_still_raises():
    missing = _RecordingBackend(contents=None)
    with staged_open(missing, "/x", "a", label="test") as stream:
        stream.write(b"tail")
    assert missing.writes == [("/x", b"tail", False)]

    with pytest.raises(FileNotFoundError):
        staged_open(_RecordingBackend(contents=None), "/x", "r", label="test")


def test_exclusive_mode_is_forwarded_to_the_backend():
    # Absent: `x` now refuses an existing file at OPEN, so the flag's journey
    # to the backend is only observable when there is nothing there.
    backend = _RecordingBackend(contents=None)
    with staged_open(backend, "/x", "x", label="test") as stream:
        stream.write(b"new")
    assert backend.writes == [("/x", b"new", True)]


def test_a_closed_stream_does_not_upload_twice():
    backend = _RecordingBackend()
    stream = staged_open(backend, "/x", "w", label="test")
    stream.write(b"once")
    stream.close()
    stream.close()
    assert len(backend.writes) == 1


def test_a_with_block_that_raises_does_not_replace_the_destination():
    """docs/guide/transfer.md promises exactly this, and it was not true.

    `IOBase.__exit__` closes the stream, and `close()` commits, so an
    interrupted copy uploaded whatever had been staged -- usually b'',
    because a failing read raises rather than returning a partial buffer.
    """
    backend = _RecordingBackend()

    with pytest.warns(ResourceWarning, match="discarded without committing"):
        with pytest.raises(RuntimeError):
            with staged_open(backend, "/f", "w", label="test") as stream:
                stream.write(b"partial")
                raise RuntimeError("the source died mid-copy")

    assert backend.writes == []
    assert backend.contents == b"existing"


def test_a_normal_close_still_commits():
    backend = _RecordingBackend()

    with staged_open(backend, "/f", "w", label="test") as stream:
        stream.write(b"done")

    assert [value for _, value, _ in backend.writes] == [b"done"]


def test_append_mode_writes_at_the_end_after_a_seek():
    """`a`/`a+` append regardless of position, as O_APPEND does.

    Seeking to the end once at open was not enough: any seek() or read()
    moved the position and the next write() overwrote existing bytes.
    """
    backend = _RecordingBackend()

    with staged_open(backend, "/f", "a+", label="test") as stream:
        stream.seek(0)
        assert stream.read(3) == b"exi"
        stream.write(b"+new")

    assert [value for _, value, _ in backend.writes] == [b"existing+new"]


def test_append_writelines_also_lands_at_the_end():
    backend = _RecordingBackend()

    with staged_open(backend, "/f", "a+", label="test") as stream:
        stream.seek(0)
        stream.writelines([b"+a", b"+b"])

    assert [value for _, value, _ in backend.writes] == [b"existing+a+b"]


@pytest.mark.parametrize("flavour", sorted(_paths()))
def test_a_text_mode_block_that_raises_does_not_commit(flavour):
    """The guards must not depend on whether the mode carried a `b`.

    `pathlib_next.Path.open()` wraps a text mode in a plain `TextIOWrapper`
    whose `__exit__` and finalizer both close the buffer -- and for a staged
    stream that close *is* the upload, so the binary guards never ran.
    hostctl supplies its own wrapper instead.
    """
    backend = _RecordingBackend()
    path = _paths()[flavour](backend)

    with pytest.warns(ResourceWarning, match="discarded without committing"):
        with pytest.raises(RuntimeError):
            with path.open("w", encoding="utf-8") as stream:
                stream.write("partial")
                raise RuntimeError("the source died mid-copy")

    assert backend.writes == []


@pytest.mark.parametrize("flavour", sorted(_paths()))
def test_an_abandoned_text_stream_never_uploads_from_the_collector(flavour):
    backend = _RecordingBackend()
    path = _paths()[flavour](backend)

    with pytest.warns(ResourceWarning, match="discarded without committing"):
        stream = path.open("w", encoding="utf-8")
        stream.write("partial")
        del stream
        gc.collect()

    assert backend.writes == []


@pytest.mark.parametrize("flavour", sorted(_paths()))
def test_a_closed_text_stream_still_commits_once(flavour):
    backend = _RecordingBackend()
    path = _paths()[flavour](backend)

    with path.open("w", encoding="utf-8") as stream:
        stream.write("done")

    assert [value for _, value, _ in backend.writes] == [b"done"]


@pytest.mark.parametrize("flavour", sorted(_paths()))
def test_text_reads_are_unaffected(flavour):
    backend = _RecordingBackend()
    path = _paths()[flavour](backend)

    with path.open("r", encoding="utf-8") as stream:
        assert stream.read() == "existing"


def test_exclusive_open_refuses_an_existing_file_before_any_work():
    """`x` was only checked by the commit, so `open("x")` on an existing file
    succeeded and the caller's whole write was thrown away at close."""
    backend = _RecordingBackend(contents=b"existing")

    with pytest.raises(FileExistsError):
        staged_open(backend, "/x", "x", label="test")

    assert backend.writes == []


def test_a_read_only_use_of_r_plus_uploads_nothing():
    """`open("r+")`/`open("a")` committed on close whether or not anything
    was written, so reading a 512-byte header re-uploaded the whole file --
    a full transfer, a new mtime, and a clobber of concurrent changes."""
    backend = _RecordingBackend(contents=b"0123456789")

    with staged_open(backend, "/x", "r+", label="test") as stream:
        assert stream.read(4) == b"0123"

    with staged_open(backend, "/x", "a", label="test"):
        pass

    assert backend.writes == []


def test_a_failed_commit_keeps_the_staged_bytes_for_a_retry():
    """The buffer was closed in a `finally`, so after a transient transport
    error the staged bytes were unrecoverable -- and a retried `close()`
    returned silently having written nothing."""
    attempts = []

    class _Flaky(_RecordingBackend):
        def write_bytes(self, path, value, *, exclusive=False):
            attempts.append(value)
            if len(attempts) == 1:
                raise ConnectionResetError("dropped")
            super().write_bytes(path, value, exclusive=exclusive)

    backend = _Flaky(contents=b"")
    stream = staged_open(backend, "/x", "w", label="test")
    stream.write(b"payload")

    with pytest.raises(ConnectionResetError):
        stream.close()

    assert stream.getvalue() == b"payload"
    stream.close()
    assert backend.writes == [("/x", b"payload", False)]


def test_copy_from_follows_the_stdlib_contract(tmp_path):
    """CPython 3.14 calls `destination._copy_from(source, follow_symlinks=,
    preserve_metadata=)` and expects it to OVERWRITE and to recurse into a
    directory. Four copies of that hook did neither: they raised
    `FileExistsError` unless given an `overwrite` keyword stdlib never
    passes, and opened a directory `"rb"`."""
    from pathlib_next import Path as LocalPath

    from hostctl.host._staged_io import copy_from

    source = LocalPath(str(tmp_path / "source.bin"))
    source.write_bytes(b"new")
    target = LocalPath(str(tmp_path / "target.bin"))
    target.write_bytes(b"old")

    copy_from(target, source)

    assert target.read_bytes() == b"new"

    tree = LocalPath(str(tmp_path / "tree"))
    tree.mkdir()
    LocalPath(str(tmp_path / "tree" / "child.txt")).write_bytes(b"child")
    copied = LocalPath(str(tmp_path / "copied"))

    copy_from(copied, tree)

    assert LocalPath(str(tmp_path / "copied" / "child.txt")).read_bytes() == b"child"


def test_copy_from_still_honours_an_explicit_no_overwrite(tmp_path):
    from pathlib_next import Path as LocalPath

    from hostctl.host._staged_io import copy_from

    source = LocalPath(str(tmp_path / "source.bin"))
    source.write_bytes(b"new")
    target = LocalPath(str(tmp_path / "target.bin"))
    target.write_bytes(b"old")

    with pytest.raises(FileExistsError):
        copy_from(target, source, overwrite=False)
