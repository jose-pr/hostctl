"""A composite path answers `pathapi.protocols.supports()` from its providers."""

from pathapi.mempath import MemPath, MemPathBackend
from pathapi.protocols import (
    BinaryOpen,
    Chmod,
    Create,
    Hardlink,
    Move,
    Remove,
    Stat,
    Symlink,
    supports,
)

from hostctl import PathProvider, PosixHost
from hostctl.provider.transports import FULL_PATH_OPERATIONS, DownloadPathProvider

_MUTATIONS = (Create, Remove, Move, Chmod, Symlink)


def _providers():
    backend = MemPathBackend()
    full = PathProvider(
        "full",
        lambda *parts: MemPath(*parts, backend=backend),
        capabilities=FULL_PATH_OPERATIONS | {"symlink_to", "readlink"},
    )
    read_only = DownloadPathProvider(lambda *parts: MemPath(*parts, backend=backend))
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
