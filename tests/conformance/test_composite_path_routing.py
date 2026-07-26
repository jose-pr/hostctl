"""Provider-retention cases which cannot be covered by one backend alone."""

from pathlib_next.mempath import MemPath, MemPathBackend

from hostctl import HostPath, PathProvider, PosixHost, ProviderProbe


def _memory_provider(name, backend, **options):
    return PathProvider(
        name,
        lambda *parts: MemPath(*parts, backend=backend),
        **options,
    )


def test_via_current_provider_creates_a_pinned_path():
    first = _memory_provider("first", MemPathBackend())
    second = _memory_provider("second", MemPathBackend())
    path = PosixHost(path_providers=(first, second)).path("value")

    pinned = path.via("first")

    assert pinned is not path
    assert pinned.provider is first
    assert pinned._pinned is True
    assert path._pinned is False


def test_selection_trace_belongs_to_the_path_instance():
    state = {"first": True}
    first = _memory_provider(
        "first",
        MemPathBackend(),
        probe=lambda: ProviderProbe("available" if state["first"] else "unavailable"),
    )
    second = _memory_provider("second", MemPathBackend())
    host = PosixHost(path_providers=(first, second))
    first_path = host.path("first")
    second_path = host.path("second")

    assert first_path.exists() is False
    first_trace = first_path.selection_trace
    state["first"] = False
    host._path_selector.invalidate()
    assert second_path.exists() is False

    assert first_trace[-1]["provider"] == "first"
    assert first_path.selection_trace == first_trace
    assert second_path.selection_trace[-1]["provider"] == "second"


def test_rename_resolves_target_on_the_selected_source_provider(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = PathProvider(
        "first",
        lambda *parts: HostPath(first_root, *parts),
        capabilities=("read",),
    )
    second = PathProvider(
        "second",
        lambda *parts: HostPath(second_root, *parts),
    )
    HostPath(second_root, "source").write_bytes(b"payload")
    host = PosixHost(path_providers=(first, second))

    renamed = host.path("source").rename(host.path("target"))

    assert renamed.provider is second
    assert renamed._pinned is True
    assert renamed.read_bytes() == b"payload"
    assert not HostPath(second_root, "source").exists()
    assert not HostPath(first_root, "target").exists()
