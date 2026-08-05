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
