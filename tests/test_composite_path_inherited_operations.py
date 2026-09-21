"""Composite paths forward the called method and inherit the rest.

Two properties, and they pull against each other:

1. A backend that overrides a derived operation for a transport-native
   implementation must be the code that runs -- ``SftpPath.copy`` fans out
   over asyncssh workers, ``SftpPath.rm``/``checksum`` execute server-side,
   ``LocalPath`` reaches ``shutil``/``os.scandir``. Decomposing a call into
   backend primitives would still produce correct results while silently
   discarding every one of those, which no output assertion would catch.
2. An operation hostctl never declares must still work, so that following
   ``pathlib_next`` does not mean hand-writing a forwarder per release.

The tests below pin both, because satisfying either one alone is easy and
the failure mode of trading one for the other is invisible.
"""

import inspect

import pytest
from pathlib_next.mempath import MemPath, MemPathBackend

from hostctl import PathProvider, PosixHost

FULL = PathProvider.DEFAULT_CAPABILITIES | {"symlink_to", "readlink", "scandir"}


class RecordingMemPath(MemPath):
    """A backend that overrides derived operations, as real backends do."""

    calls: list = []

    def copy(self, target, **kwargs):
        type(self).calls.append("copy")
        return super().copy(target, **kwargs)

    def rm(self, *args, **kwargs):
        type(self).calls.append("rm")
        return super().rm(*args, **kwargs)

    def touch(self, mode=0o666, exist_ok=True):
        type(self).calls.append("touch")
        return super().touch(mode=mode, exist_ok=exist_ok)


@pytest.fixture
def host():
    backend = MemPathBackend()
    RecordingMemPath("root", backend=backend).mkdir()
    RecordingMemPath("root/a.txt", backend=backend).write_bytes(b"a")
    RecordingMemPath.calls = []
    provider = PathProvider(
        "recording",
        lambda *p: RecordingMemPath(*p, backend=backend),
        capabilities=FULL,
    )
    return PosixHost(path_providers=(provider,))


def test_backend_override_of_copy_runs(host):
    """The backend's own copy() must run, not a primitive decomposition."""
    source = host.path("root", "a.txt")

    source.copy(host.path("root", "b.txt"))

    assert "copy" in RecordingMemPath.calls


def test_backend_override_of_rm_runs(host):
    """rm() is a derived operation; SftpPath implements it server-side."""
    target = host.path("root", "a.txt")

    target.rm()

    assert "rm" in RecordingMemPath.calls


def test_backend_override_of_touch_runs(host):
    """touch() is derived from chmod()/open() but backends may override it."""
    host.path("root", "t.txt").touch()

    assert "touch" in RecordingMemPath.calls


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(lambda p: p.lstat(), id="lstat"),
        pytest.param(lambda p: p.is_symlink(), id="is_symlink"),
        pytest.param(lambda p: p.read_text(), id="read_text"),
        pytest.param(lambda p: p.exists(), id="exists"),
    ],
)
def test_operations_work_without_a_composite_declaration(host, operation):
    """None of these need a hand-written method in composite_path.py."""
    operation(host.path("root", "a.txt"))


def test_chown_reaches_the_backend_without_a_composite_method(host):
    """The regression that motivated this: 0.9.1 added chown(), hostctl had none.

    The assertion is that the call *reaches the backend* -- MemPath has no
    ``_chown`` primitive, so NotImplementedError from it is the proof that
    routing happened rather than a missing attribute at the composite layer.
    """
    from pathlib_next import Path as PnPath

    if not hasattr(PnPath, "chown"):
        pytest.skip("installed pathlib_next predates chown()")

    from hostctl.host import composite_path

    assert "def chown" not in inspect.getsource(composite_path)

    with pytest.raises(NotImplementedError) as excinfo:
        host.path("root", "a.txt").chown(uid=0)

    assert "_chown" in str(excinfo.value)


def test_walk_yields_composite_paths(host):
    """A derived traversal must still hand back routed paths, not raw ones."""
    for root, _dirs, _files in host.path("root").walk():
        assert root.provider is not None


def test_a_composite_argument_keeps_its_backend(tmp_path):
    """A composite argument reaches the backend as one of its own paths.

    Composite arguments used to be stringified for every forwarder, so the
    backend rebuilt the path from bare text through its own constructor --
    and for a backend whose paths carry state that constructor FAILS
    (`TypeError: WinRMPath requires a backend`), which is not even the
    `NotImplementedError` the row's retry contract is written against.
    `pathlib_next` 0.9.10 closed that particular hole by routing through
    `with_segments`, so this is a guard rather than a reproduction: hostctl
    now hands over the provider's own path and does not rely on the backend
    re-parsing text correctly.
    """
    import os

    from hostctl.host._winrm import WinRMPath

    class _Backend:
        """A backend whose stat carries st_dev/st_ino, as a real one may.

        `samefile` compares those, so it reaches the ARGUMENT -- which is
        the half this test is about.
        """

        def __init__(self, sample):
            self._sample = os.stat(sample)

        def stat(self, path, *, follow_symlinks=True):
            return self._sample

    (tmp_path / "sample").write_bytes(b"value")
    backend = _Backend(tmp_path / "sample")
    provider = PathProvider(
        "winrm",
        lambda *p: WinRMPath(*p, backend=backend),
        capabilities=("stat",),
    )
    host = PosixHost(path_providers=(provider,))

    first = host.path(r"C:\data.bin")
    second = host.path(r"C:\data.bin")

    assert first.samefile(second) is True


def test_a_download_provider_answers_the_stat_predicates_it_derives():
    """It declared `stat` and `exists` but not `is_file`/`is_dir`, so a
    pinned download path answered `stat()` and `is_symlink()` and refused
    `is_file()` -- and every pathlib_next helper that asks `is_dir()` first
    broke on it."""
    from hostctl.provider.transports import DownloadPathProvider

    backend = MemPathBackend()
    MemPath("payload", backend=backend).write_bytes(b"value")
    download = DownloadPathProvider(lambda *p: MemPath(*p, backend=backend))
    host = PosixHost(path_providers=(download,))

    path = host.path("payload").via("download")

    assert path.stat().st_size == 5
    assert path.is_file() is True
    assert path.is_dir() is False


def test_a_read_only_provider_can_still_copy_out(tmp_path):
    """`copy`/`move` were both gated on the source provider's `write`, so a
    read-only provider could not copy OUT at all -- although only reads
    happen on the source -- while `read_bytes()` on the same path worked."""
    from pathlib_next import Path as LocalPath
    from hostctl.provider.transports import DownloadPathProvider

    backend = MemPathBackend()
    MemPath("payload", backend=backend).write_bytes(b"value")
    host = PosixHost(
        path_providers=(DownloadPathProvider(lambda *p: MemPath(*p, backend=backend)),)
    )
    target = LocalPath(str(tmp_path / "copied"))

    host.path("payload").copy(target)

    assert target.read_bytes() == b"value"


def test_a_backend_move_still_answers_with_a_composite_path():
    """A backend's own `move()` returned ITS path type, which escapes
    composite routing and pinning -- while `rename()` on the same host
    returned a composite."""

    class _MovingMemPath(MemPath):
        def move(self, target, **kwargs):
            data = self.read_bytes()
            target.write_bytes(data)
            self.unlink()
            return target

    backend = MemPathBackend()
    MemPath("from.bin", backend=backend).write_bytes(b"value")
    host = PosixHost(
        path_providers=(
            PathProvider("memory", lambda *p: _MovingMemPath(*p, backend=backend)),
        )
    )

    moved = host.path("from.bin").move(host.path("to.bin"))

    assert isinstance(moved, type(host.path("to.bin")))
    assert moved.read_bytes() == b"value"


def test_a_refused_operation_leaves_the_host_usable():
    """A `NotImplementedError` says "this provider cannot do THIS", not "this
    provider is unusable": declining it on the host's shared selector took
    the provider out for every later operation, so a backend refusing
    `samefile()` made the next read fail with "no path provider supports
    open_read"."""
    backend = MemPathBackend()
    MemPath("data.bin", backend=backend).write_bytes(b"value")

    class _NoSamefile(MemPath):
        def samefile(self, other):
            raise NotImplementedError("samefile is unsupported here")

    host = PosixHost(
        path_providers=(
            PathProvider("memory", lambda *p: _NoSamefile(*p, backend=backend)),
        )
    )
    path = host.path("data.bin")

    with pytest.raises(NotImplementedError):
        path.samefile(host.path("data.bin"))

    assert host.path("data.bin").read_bytes() == b"value"


def test_the_default_path_syncer_policy_works_over_composite_paths(tmp_path):
    """`PathSyncer`'s default checksum calls `supported_checksums()` and
    `is_local()` bare, so either one raising aborts a whole sync on the first
    file."""
    from pathlib_next import Path as LocalPath
    from pathlib_next.utils.sync import PathSyncer

    source_root = LocalPath(str(tmp_path / "source"))
    source_root.mkdir()
    LocalPath(str(tmp_path / "source" / "a.txt")).write_bytes(b"a")
    target_root = LocalPath(str(tmp_path / "target"))
    target_root.mkdir()

    host = PosixHost(path_providers=(PathProvider("local", lambda *p: LocalPath(*p)),))

    PathSyncer().sync(host.path(str(source_root)), host.path(str(target_root)))

    assert LocalPath(str(tmp_path / "target" / "a.txt")).read_bytes() == b"a"
