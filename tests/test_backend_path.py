"""The shared base of the WinRM, container and QGA path classes."""

import errno
import io
import tarfile

import pytest

from hostctl.host._backend_path import BackendPathMixin
from hostctl.host._winrm import _SCRIPT_BUDGET, WinRMPath, WinRMPathBackend
from hostctl.host.container_path import ContainerPathBackend, PosixContainerPath
from hostctl.host.qemu import PosixQEMUPath
from tests.conformance.path_fakes import WinRMFilesystemRunner
from tests.conformance.providers import FakeQEMUHost


@pytest.fixture
def qga_pair():
    hosts = [FakeQEMUHost(), FakeQEMUHost()]
    for host in hosts:
        host.path("/")
    yield [host.path_provider.backend for host in hosts]
    for host in hosts:
        host._conformance_cleanup()


@pytest.fixture
def winrm_pair():
    runners = [WinRMFilesystemRunner(), WinRMFilesystemRunner()]
    yield [
        WinRMPathBackend(runner, max_script_bytes=_SCRIPT_BUDGET) for runner in runners
    ]
    for runner in runners:
        runner.close()


def _qga(backend, name):
    return PosixQEMUPath(f"/{name}", backend=backend)


def _winrm(backend, name):
    return WinRMPath(f"C:/{name}", backend=backend)


CASES = [("qga_pair", _qga), ("winrm_pair", _winrm)]


@pytest.mark.parametrize("fixture, make", CASES)
def test_move_between_two_hosts_of_one_transport_copies(request, fixture, make):
    first, second = request.getfixturevalue(fixture)
    source = make(first, "a.txt")
    source.write_bytes(b"payload")
    target = make(second, "b.txt")

    source.move(target)

    assert target.read_bytes() == b"payload"
    assert not source.exists()


@pytest.mark.parametrize("fixture, make", CASES)
def test_rename_across_backends_is_exdev(request, fixture, make):
    first, second = request.getfixturevalue(fixture)
    source = make(first, "a.txt")
    source.write_bytes(b"x")

    with pytest.raises(OSError) as raised:
        source.rename(make(second, "b.txt"))

    assert raised.value.errno == errno.EXDEV
    assert source.exists()


@pytest.mark.parametrize("fixture, make", CASES)
def test_chmod_takes_a_mode_string(request, fixture, make):
    first, _ = request.getfixturevalue(fixture)
    path = make(first, "m.txt")
    path.write_bytes(b"x")
    calls = []
    original = type(first).chmod

    def spy(self, target, mode, **kwargs):
        calls.append(mode)
        return original(self, target, mode, **kwargs)

    type(first).chmod = spy
    try:
        path.chmod("755")
        path.chmod(0o755)
    finally:
        type(first).chmod = original

    assert calls == [0o755, 0o755]


def test_a_foreign_segment_lends_no_backend(qga_pair):
    container = PosixContainerPath("/x", backend=ContainerPathBackend(object()))
    with pytest.raises(TypeError, match="requires a backend"):
        PosixQEMUPath(container)
    assert PosixQEMUPath(_qga(qga_pair[0], "x")).backend is qga_pair[0]


def test_iterdir_drops_unsafe_names(qga_pair):
    backend = qga_pair[0]
    listing = [("..", None), ("ok", None), ("a/b", None)]
    backend.scandir = lambda path: listing
    assert [p.name for p in PosixQEMUPath("/", backend=backend).iterdir()] == ["ok"]

    runner = WinRMFilesystemRunner()
    try:
        winrm = WinRMPathBackend(runner, max_script_bytes=_SCRIPT_BUDGET)
        winrm.scandir = lambda path: [("..", None), ("ok", None), ("a\\b", None)]
        assert [p.name for p in WinRMPath("C:/", backend=winrm).iterdir()] == ["ok"]
    finally:
        runner.close()


def test_iterdir_drops_unsafe_names_in_containers():
    backend = ContainerPathBackend(object())
    backend.scandir = lambda path: [("..", None), ("ok", None)]
    assert [p.name for p in PosixContainerPath("/", backend=backend).iterdir()] == [
        "ok"
    ]


def test_the_base_is_shared():
    for cls in (PosixQEMUPath, WinRMPath, PosixContainerPath):
        assert issubclass(cls, BackendPathMixin)
        for name in ("copy", "move", "_symlink_target"):
            assert name not in vars(cls)
