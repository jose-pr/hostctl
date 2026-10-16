"""A composite path answers `pathapi.protocols.supports()` from its providers."""

import os

from pathapi.mempath import MemPath, MemPathBackend
from pathapi.protocols import (
    BinaryOpen,
    Chmod,
    Chown,
    Create,
    Hardlink,
    Move,
    NativeChecksum,
    Owned,
    Remove,
    Resolve,
    Stat,
    Symlink,
    Utime,
    supports,
)

from hostctl.provider._contracts import PathProvider
from hostctl.system._system import PosixHost
from hostctl.provider._contracts import FULL_PATH_OPERATIONS, READ_ONLY_PATH_OPERATIONS

_MUTATIONS = (Create, Remove, Move, Chmod, Symlink)


def _providers():
    backend = MemPathBackend()
    full = PathProvider(
        "full",
        lambda *parts: MemPath(*parts, backend=backend),
        capabilities=FULL_PATH_OPERATIONS | {"symlink_to", "readlink"},
    )
    read_only = PathProvider(
        "download",
        lambda *parts: MemPath(*parts, backend=backend),
        capabilities=READ_ONLY_PATH_OPERATIONS,
    )
    return read_only, full


def test_a_read_only_provider_supports_no_mutation():
    read_only, _ = _providers()
    path = PosixHost(path_providers=(read_only,)).path("/x")

    assert supports(path, Stat)
    assert supports(path, BinaryOpen)
    for protocol in _MUTATIONS:
        assert not supports(path, protocol), protocol


def test_any_provider_the_path_may_route_to_counts():
    read_only, full = _providers()
    path = PosixHost(path_providers=(read_only, full)).path("/x")

    for protocol in (Stat, BinaryOpen) + _MUTATIONS:
        assert supports(path, protocol), protocol


def test_a_pinned_path_answers_for_its_provider_alone():
    read_only, full = _providers()
    path = PosixHost(path_providers=(read_only, full)).path("/x")

    assert not supports(path.via("download"), Remove)
    assert supports(path.via("full"), Remove)


def test_a_protocol_the_composite_does_not_forward_is_the_class_answer():
    _, full = _providers()
    path = PosixHost(path_providers=(full,)).path("/x")

    assert not supports(path, Hardlink)


def _local_host():
    from pathapi import LocalPath

    from hostctl.system._system import WindowsHost

    provider = PathProvider("local", LocalPath, capabilities=FULL_PATH_OPERATIONS)
    host_class = WindowsHost if os.name == "nt" else PosixHost
    return host_class(path_providers=(provider,))


def test_a_composite_over_a_local_path_answers_for_the_local_class(tmp_path):
    path = _local_host().path(str(tmp_path / "f.txt"))

    for protocol in (Utime, Hardlink, Owned, Resolve, Chown):
        assert supports(path, protocol), protocol
    # LocalPath has no server-side digest, so the composite claims none.
    assert not supports(path, NativeChecksum)


def test_a_composite_forwards_utime_hardlink_owner_group_and_resolve(tmp_path):
    path = _local_host().path(str(tmp_path / "f.txt"))
    path.write_bytes(b"x")

    path._utime(mtime=1_000_000_000)
    assert int(path.stat().st_mtime) == 1_000_000_000

    link = path.with_name("link.txt")
    link.hardlink_to(path)
    assert link.read_bytes() == b"x"

    if os.name == "posix":
        assert isinstance(path.owner(), str)
        assert isinstance(path.group(), str)

    resolved = path.resolve()
    assert isinstance(resolved, type(path))
    assert resolved.provider is path.provider


def test_a_relative_path_made_absolute_stays_a_composite(tmp_path, monkeypatch):
    host = _local_host()
    monkeypatch.chdir(tmp_path)

    relative = host.path("f.txt")
    absolute = relative.absolute()

    assert isinstance(absolute, type(relative))
    assert absolute.is_absolute()
    assert absolute.provider is relative.provider
    assert absolute.absolute() is absolute


def test_chown_and_checksum_follow_the_backend_class():
    backend = MemPathBackend()
    provider = PathProvider(
        "mem",
        lambda *parts: MemPath(*parts, backend=backend),
        capabilities=FULL_PATH_OPERATIONS,
    )
    path = PosixHost(path_providers=(provider,)).path("/x")
    cls = type(path._provider_path(provider))

    for protocol in (Chown, NativeChecksum, Utime, Hardlink, Owned, Resolve):
        assert supports(path, protocol) == supports(cls, protocol), protocol
