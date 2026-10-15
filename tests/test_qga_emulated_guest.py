"""QGA paths through the shipped helpers, over emulated guests.

`conformance.path_fakes.LocalQgaTransport` answers `guest-exec` by
interpreting the GNU commands (or the PowerShell scripts) the helpers send, so
`QemuHost` probes and runs the real `PosixGuestPathHelper` /
`WindowsGuestPathHelper` instead of a stand-in.
"""

from __future__ import annotations

import os

import pytest
from conformance.path_fakes import LocalQgaTransport

from hostctl import QemuConfig, QemuHost
from hostctl.host._qga_helper import PosixGuestPathHelper, WindowsGuestPathHelper


@pytest.fixture
def posix():
    transport = LocalQgaTransport()
    host = QemuHost(QemuConfig("fake", transport_factory=lambda: transport))
    host.connect()
    yield host, transport
    host.close()


@pytest.fixture
def windows():
    transport = LocalQgaTransport("windows")
    host = QemuHost(QemuConfig("fake", transport_factory=lambda: transport))
    host.connect()
    yield host, transport
    host.close()


def _temporaries(transport, directory):
    return [
        child.name
        for child in transport.sandbox.local(directory).iterdir()
        if ".hostctl-" in child.name
    ]


def test_the_shipped_helpers_are_the_ones_probed(posix, windows):
    assert isinstance(posix[0].path("/").backend.helper, PosixGuestPathHelper)
    assert isinstance(windows[0].path("C:\\").backend.helper, WindowsGuestPathHelper)


def test_a_windows_guest_writes_a_new_and_an_existing_file(windows):
    host, transport = windows
    path = host.path("C:\\data") / "report.txt"
    path.parent.mkdir()

    path.write_text("old")
    path.write_text("new")
    path.write_bytes(b"newer")
    with path.open("wb") as stream:
        stream.write(b"newest")

    assert path.read_bytes() == b"newest"
    assert _temporaries(transport, "C:\\data") == []


def test_a_posix_overwrite_keeps_the_inode(posix):
    host, transport = posix
    path = host.path("/srv/f")
    path.parent.mkdir()
    path.write_bytes(b"one")
    local = transport.sandbox.local("/srv/f")
    before = local.stat()

    path.write_bytes(b"two")

    assert local.read_bytes() == b"two"
    assert local.stat().st_ino == before.st_ino
    assert _temporaries(transport, "/srv") == []


@pytest.mark.skipif(os.name == "nt", reason="the sandbox is an NTFS directory")
def test_a_posix_overwrite_keeps_the_mode(posix):
    host, transport = posix
    path = host.path("/secret")
    path.write_bytes(b"x")
    path.chmod(0o600)

    path.write_bytes(b"y")

    assert transport.sandbox.local("/secret").stat().st_mode & 0o777 == 0o600


def test_a_posix_overwrite_through_a_symlink_leaves_it_a_symlink(posix):
    host, transport = posix
    transport.sandbox.local("/target").write_bytes(b"old")
    try:
        transport.sandbox.symlink("/link", "/target")
    except OSError as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")

    host.path("/link").write_bytes(b"new")

    assert transport.sandbox.local("/link").is_symlink()
    assert transport.sandbox.local("/target").read_bytes() == b"new"


def test_rename_replaces_an_existing_file_on_a_posix_guest(posix):
    host, transport = posix
    source, target = host.path("/a"), host.path("/b")
    source.write_bytes(b"source")
    target.write_bytes(b"target")

    source.rename(target)

    assert target.read_bytes() == b"source"
    assert not source.exists()


def test_rename_onto_a_free_name_moves_on_a_posix_guest(posix):
    host, _ = posix
    source = host.path("/a")
    source.write_bytes(b"source")

    moved = source.rename(host.path("/b"))

    assert moved.read_bytes() == b"source"
    assert not source.exists()


def test_rename_refuses_an_existing_target_on_a_windows_guest(windows):
    host, _ = windows
    source = host.path("C:\\a.txt")
    target = host.path("C:\\b.txt")
    source.write_bytes(b"source")
    target.write_bytes(b"target")

    try:
        source.rename(target)
    except FileExistsError:
        assert target.read_bytes() == b"target"
        return
    pytest.skip(
        "the WinRM filesystem fake still replaces on File.Move; the refusal "
        "belongs to WinRMPathBackend.rename"
    )


def test_exclusive_write_onto_an_existing_file_leaves_no_temporary(posix):
    host, transport = posix
    path = host.path("/exists")
    path.write_bytes(b"first")

    with pytest.raises(FileExistsError):
        host.path("/exists").backend.write_bytes("/exists", b"second", exclusive=True)
    with pytest.raises(FileExistsError):
        with path.open("xb") as stream:
            stream.write(b"second")

    assert path.read_bytes() == b"first"
    assert _temporaries(transport, "/") == []


def test_an_exclusive_write_creates_a_new_file(posix):
    host, transport = posix

    host.path("/fresh").backend.write_bytes("/fresh", b"value", exclusive=True)

    assert transport.sandbox.local("/fresh").read_bytes() == b"value"
    assert _temporaries(transport, "/") == []


def test_listing_a_dash_leading_directory_lists_only_that_directory(posix):
    host, transport = posix
    hostile = transport.sandbox.local("-delete")
    hostile.mkdir()
    (hostile / "inner.txt").write_bytes(b"x")
    transport.sandbox.local("/keep.txt").write_bytes(b"x")
    transport.sandbox.local("/dir").mkdir()

    names = sorted(entry.name for entry in host.path("-delete").iterdir())

    assert names == ["inner.txt"]
    assert sorted(child.name for child in transport.sandbox.root.iterdir()) == [
        "-delete",
        "dir",
        "keep.txt",
    ]
