"""The staged write-back stream shared by the whole-file path backends.

WinRM, the Docker archive API, and the QGA file RPCs each emulate a writable
`open()` by staging bytes in memory and uploading on `close()`, through
one shared stream. An abandoned write stream must not upload from the garbage
collector, where any transport error is printed and swallowed by the
interpreter.

These tests pin that guard for every backend, and the `t`-suffix rule.
"""

from __future__ import annotations

import gc
import io
import sys

import pytest

from pathapi.providers import CommitOnClose

from hostctl.paths._staged_io import staged_open, validate_open_mode
from hostctl.transports.winrm._transport import WinRMPath
from hostctl.transports.container._path import PosixContainerPath
from hostctl.transports.qemu._host import PosixQEMUPath


class _RecordingBackend:
    """The whole-file interface `staged_open` needs, and nothing else."""

    def __init__(self, contents=b"existing"):
        self.contents = contents
        self.writes = []

    def read_bytes(self, path):
        if self.contents is None:
            raise FileNotFoundError(path)
        return self.contents

    def exists(self, path):
        return self.contents is not None

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
        "qga": lambda backend: PosixQEMUPath("/data.bin", backend=backend),
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
    """`"rt"` is accepted by every whole-file backend."""
    backend = _RecordingBackend(contents=b"value")
    path = _paths()[flavour](backend)

    with path.open("rt") as stream:
        assert stream.read() == "value"


def test_open_mode_validation_is_one_rule():
    # `pathapi` strips the `b` before calling `_open`, so these are the
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
    stream = staged_open(backend, "/x", "r")
    assert not isinstance(stream, CommitOnClose)
    assert stream.read() == b"streamed"

    # A backend without `open_read` still gets a staged read rather than an
    # AttributeError.
    plain = staged_open(_RecordingBackend(b"staged"), "/x", "r")
    assert plain.read() == b"staged"


def test_append_creates_a_missing_file_but_read_still_raises():
    missing = _RecordingBackend(contents=None)
    with staged_open(missing, "/x", "a") as stream:
        stream.write(b"tail")
    assert missing.writes == [("/x", b"tail", False)]

    with pytest.raises(FileNotFoundError):
        staged_open(_RecordingBackend(contents=None), "/x", "r")


def test_exclusive_mode_is_forwarded_to_the_backend():
    # Absent: `x` now refuses an existing file at OPEN, so the flag's journey
    # to the backend is only observable when there is nothing there.
    backend = _RecordingBackend(contents=None)
    with staged_open(backend, "/x", "x") as stream:
        stream.write(b"new")
    assert backend.writes == [("/x", b"new", True)]


def test_a_closed_stream_does_not_upload_twice():
    backend = _RecordingBackend()
    stream = staged_open(backend, "/x", "w")
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

    with pytest.raises(RuntimeError):
        with staged_open(backend, "/f", "w") as stream:
            stream.write(b"partial")
            raise RuntimeError("the source died mid-copy")

    assert backend.writes == []
    assert backend.contents == b"existing"


def test_a_normal_close_still_commits():
    backend = _RecordingBackend()

    with staged_open(backend, "/f", "w") as stream:
        stream.write(b"done")

    assert [value for _, value, _ in backend.writes] == [b"done"]


def test_append_mode_writes_at_the_end_after_a_seek():
    """`a`/`a+` append regardless of position, as O_APPEND does.

    Seeking to the end once at open was not enough: any seek() or read()
    moved the position and the next write() overwrote existing bytes.
    """
    backend = _RecordingBackend()

    with staged_open(backend, "/f", "a+") as stream:
        stream.seek(0)
        assert stream.read(3) == b"exi"
        stream.write(b"+new")

    assert [value for _, value, _ in backend.writes] == [b"existing+new"]


def test_append_writelines_also_lands_at_the_end():
    backend = _RecordingBackend()

    with staged_open(backend, "/f", "a+") as stream:
        stream.seek(0)
        stream.writelines([b"+a", b"+b"])

    assert [value for _, value, _ in backend.writes] == [b"existing+a+b"]


@pytest.mark.parametrize("flavour", sorted(_paths()))
def test_a_text_mode_block_that_raises_does_not_commit(flavour):
    """The guards must not depend on whether the mode carried a `b`.

    A stock `TextIOWrapper`'s `__exit__` and finalizer close the buffer, and
    for a staged stream that close *is* the upload; pathapi's `open()` wraps a
    stream that has `discard()` in a wrapper that discards instead.
    """
    backend = _RecordingBackend()
    path = _paths()[flavour](backend)

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
        staged_open(backend, "/x", "x")

    assert backend.writes == []


def test_a_read_only_use_of_r_plus_uploads_nothing():
    """`open("r+")`/`open("a")` committed on close whether or not anything
    was written, so reading a 512-byte header re-uploaded the whole file --
    a full transfer, a new mtime, and a clobber of concurrent changes."""
    backend = _RecordingBackend(contents=b"0123456789")

    with staged_open(backend, "/x", "r+") as stream:
        assert stream.read(4) == b"0123"

    with staged_open(backend, "/x", "a"):
        pass

    assert backend.writes == []


def test_a_failed_commit_closes_the_stream_and_is_not_retried():
    """A stream left open after a failed upload was collected later and
    uploaded again over whatever had changed remotely; the caller writes
    again to retry."""
    attempts = []

    class _Flaky(_RecordingBackend):
        def write_bytes(self, path, value, *, exclusive=False):
            attempts.append(value)
            raise ConnectionResetError("dropped")

    backend = _Flaky(contents=b"")
    stream = staged_open(backend, "/x", "w")
    stream.write(b"payload")

    with pytest.raises(ConnectionResetError):
        stream.close()

    assert stream.closed is True
    stream.close()
    assert attempts == [b"payload"]


def test_w_with_no_writes_still_creates_an_empty_file():
    backend = _RecordingBackend(contents=b"old")
    staged_open(backend, "/x", "w").close()
    assert backend.writes == [("/x", b"", False)]


def test_r_plus_with_no_writes_uploads_nothing():
    backend = _RecordingBackend(contents=b"old")
    with staged_open(backend, "/x", "r+") as stream:
        assert stream.read(2) == b"ol"
    assert backend.writes == []


def test_append_writes_land_at_the_end_after_a_seek():
    backend = _RecordingBackend(contents=b"old")
    with staged_open(backend, "/x", "a") as stream:
        stream.seek(0)
        stream.write(b"+new")
    assert backend.writes == [("/x", b"old+new", False)]


@pytest.mark.parametrize("mode", ["w", "r+", "a"])
def test_a_block_that_raises_uploads_nothing(mode):
    backend = _RecordingBackend(contents=b"old")
    with pytest.raises(RuntimeError):
        with staged_open(backend, "/x", mode) as stream:
            stream.write(b"never sent")
            raise RuntimeError("boom")
    assert backend.writes == []


def test_text_mode_over_a_staged_stream_commits_once():
    path = WinRMPath(r"C:\data.txt", backend=(backend := _RecordingBackend(b"")))
    with path.open("w") as stream:
        stream.write("text")
    assert backend.writes == [(r"C:\data.txt", b"text", False)]

    with pytest.raises(RuntimeError):
        with path.open("w") as stream:
            stream.write("lost")
            raise RuntimeError("boom")
    assert len(backend.writes) == 1


def test_copy_from_follows_the_stdlib_contract(tmp_path):
    """CPython 3.14 calls `destination._copy_from(source, follow_symlinks=,
    preserve_metadata=)` and expects it to OVERWRITE and to recurse into a
    directory. Four copies of that hook did neither: they raised
    `FileExistsError` unless given an `overwrite` keyword stdlib never
    passes, and opened a directory `"rb"`."""
    from pathapi import Path as LocalPath

    from hostctl.paths._staged_io import copy_from

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
    from pathapi import Path as LocalPath

    from hostctl.paths._staged_io import copy_from

    source = LocalPath(str(tmp_path / "source.bin"))
    source.write_bytes(b"new")
    target = LocalPath(str(tmp_path / "target.bin"))
    target.write_bytes(b"old")

    with pytest.raises(FileExistsError):
        copy_from(target, source, overwrite=False)


class _ExistsBackend:
    def __init__(self, answer):
        self.answer = answer

    def exists(self, path):
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def write_bytes(self, path, value, *, exclusive=False):
        raise AssertionError("nothing may be written")


def test_exclusive_open_does_not_call_a_server_error_exists():
    class Boom(OSError):
        pass

    with pytest.raises(Boom):
        staged_open(_ExistsBackend(Boom("500")), "/x", "x")


def test_exclusive_open_of_a_missing_path_proceeds():
    stream = staged_open(_ExistsBackend(False), "/x", "x")

    stream.discard()


def test_exclusive_open_of_an_existing_path_is_file_exists():
    with pytest.raises(FileExistsError):
        staged_open(_ExistsBackend(True), "/x", "x")


def test_exclusive_open_needs_the_backends_exists():
    class _NoExists:
        def write_bytes(self, path, value, *, exclusive=False):
            raise AssertionError("nothing may be written")

    with pytest.raises(NotImplementedError, match="exists"):
        staged_open(_NoExists(), "/x", "x")


class _Child:
    def __init__(self, name, data=b"x"):
        self.name = name
        self._data = data

    def is_dir(self):
        return False

    def is_symlink(self):
        return False

    def open(self, mode="rb"):
        import io

        return io.BytesIO(self._data)

    def stat(self):
        raise NotImplementedError


class _Tree(_Child):
    def __init__(self, *children):
        super().__init__("tree")
        self._children = children

    def is_dir(self):
        return True

    def iterdir(self):
        return iter(self._children)


def _windows_destination(tmp_path):
    from hostctl.transports.winrm._transport import (
        _SCRIPT_BUDGET,
        WinRMPath,
        WinRMPathBackend,
    )
    from tests.conformance.path_fakes import WinRMFilesystemRunner

    runner = WinRMFilesystemRunner()
    backend = WinRMPathBackend(runner, max_script_bytes=_SCRIPT_BUDGET)
    return runner, WinRMPath("C:/dst", backend=backend)


@pytest.mark.parametrize("name", ["..\\escaped.txt", "..", "a/b", "C:evil"])
def test_copy_from_refuses_a_child_name_that_is_not_one_component(tmp_path, name):
    from hostctl.paths._staged_io import copy_from

    runner, destination = _windows_destination(tmp_path)
    try:
        with pytest.raises(ValueError, match="refusing unsafe child name"):
            copy_from(destination, _Tree(_Child("ok.txt"), _Child(name)))
        assert not destination.parent.joinpath("escaped.txt").exists()
    finally:
        runner.close()


def test_copy_from_refuses_an_existing_destination_directory(tmp_path):
    from hostctl.paths._staged_io import copy_from

    runner, destination = _windows_destination(tmp_path)
    try:
        destination.mkdir()
        with pytest.raises(FileExistsError):
            copy_from(destination, _Tree(_Child("ok.txt")))
    finally:
        runner.close()


@pytest.mark.skipif(
    sys.version_info < (3, 14) or sys.platform == "win32",
    reason="stdlib Path.copy is 3.14+; a POSIX source can hold a backslash name",
)
def test_stdlib_copy_into_a_windows_host_path_cannot_escape(tmp_path):
    import pathlib

    source = tmp_path / "src"
    source.mkdir()
    (source / "..\\escaped.txt").write_bytes(b"x")
    runner, destination = _windows_destination(tmp_path)
    try:
        with pytest.raises(ValueError, match="refusing unsafe child name"):
            pathlib.Path(source).copy(destination)
        assert not destination.parent.joinpath("escaped.txt").exists()
    finally:
        runner.close()


def test_copy_from_preserves_mode_and_mtime_where_the_destination_can(tmp_path):
    import stat as _stat

    from hostctl.paths._staged_io import copy_from

    calls = []

    class Source(_Child):
        def stat(self):
            class _Stat:
                st_mode = _stat.S_IFREG | 0o640
                st_mtime = 1234.5

            return _Stat()

    class Destination:
        def exists(self):
            return False

        def open(self, mode="wb"):
            return io.BytesIO()

        def chmod(self, mode):
            calls.append(("chmod", mode))

        def _utime(self, **kwargs):
            calls.append(("utime", kwargs))
            raise NotImplementedError

    copy_from(Destination(), Source("f"), preserve_metadata=True)

    assert calls == [("chmod", 0o640), ("utime", {"mtime": 1234.5})]
