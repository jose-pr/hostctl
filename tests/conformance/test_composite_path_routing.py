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


def test_move_to_a_foreign_path_transfers_instead_of_renaming_on_the_source():
    """A foreign destination is a cross-backend transfer, not a backend rename.

    `ssh_path.move(local_path)` used to issue an SFTP rename *inside the remote
    host* to a path spelled like the local destination: the file left the
    source, never arrived, and a path was returned with no error.
    """
    from pathlib_next import LocalPath
    import tempfile

    backend = MemPathBackend()
    MemPath("export.csv", backend=backend).write_bytes(b"payload")
    host = PosixHost(path_providers=(_memory_provider("remote", backend),))

    with tempfile.TemporaryDirectory() as tmp:
        destination = LocalPath(tmp) / "export.csv"
        host.path("export.csv").move(destination)

        assert destination.read_bytes() == b"payload"
    assert not MemPath("export.csv", backend=backend).exists()
    # Nothing was renamed into place on the source host.
    assert list(backend) == []


def test_move_between_two_providers_does_not_use_either_backend_rename():
    source_backend, target_backend = MemPathBackend(), MemPathBackend()
    MemPath("data.bin", backend=source_backend).write_bytes(b"content")
    source = PosixHost(path_providers=(_memory_provider("a", source_backend),))
    target = PosixHost(path_providers=(_memory_provider("b", target_backend),))

    source.path("data.bin").move(target.path("data.bin"))

    assert MemPath("data.bin", backend=target_backend).read_bytes() == b"content"
    assert not MemPath("data.bin", backend=source_backend).exists()


def test_a_string_destination_is_a_logical_path_on_the_same_host():
    """`pathlib_next` documents `target: Path | str`; both raised TypeError."""
    backend = MemPathBackend()
    MemPath("a.txt", backend=backend).write_bytes(b"data")
    host = PosixHost(path_providers=(_memory_provider("only", backend),))

    host.path("a.txt").copy("b.txt")
    host.path("a.txt").move("c.txt")

    assert MemPath("b.txt", backend=backend).read_bytes() == b"data"
    assert MemPath("c.txt", backend=backend).read_bytes() == b"data"
    assert not MemPath("a.txt", backend=backend).exists()


def test_a_same_provider_destination_still_takes_the_backend_route():
    """The optimisation the routing exists for must survive the fix."""
    calls = []

    class RecordingMemPath(MemPath):
        def move(self, target, **kwargs):
            calls.append((str(self), str(target)))
            return super().move(target, **kwargs)

    backend = MemPathBackend()
    MemPath("one.txt", backend=backend).write_bytes(b"x")
    provider = PathProvider(
        "only", lambda *parts: RecordingMemPath(*parts, backend=backend)
    )
    host = PosixHost(path_providers=(provider,))

    host.path("one.txt").move(host.path("two.txt"))

    assert calls == [("one.txt", "two.txt")]


def test_a_joined_composite_does_not_borrow_its_parents_backend_path():
    """`CompositePosixPath(parent, "cache")` is the public pathlib idiom.

    It inherited the parent's cached backend path, so every operation went to
    the parent -- `rm(recursive=True)` on the "child" deleted the parent tree.
    """
    from hostctl import CompositePosixPath

    backend = MemPathBackend()
    for directory in ("srv", "srv/app", "srv/app/cache"):
        MemPath(directory, backend=backend).mkdir(parents=True)
    MemPath("srv/app/config.ini", backend=backend).write_bytes(b"keep me")
    MemPath("srv/app/cache/x.tmp", backend=backend).write_bytes(b"junk")
    host = PosixHost(path_providers=(_memory_provider("mem", backend),))

    parent = host.path("srv/app")
    child = CompositePosixPath(parent, "cache")

    assert str(child) == "srv/app/cache"
    assert str(child._provider_path(child.provider)) == "srv/app/cache"

    child.rm(recursive=True)

    assert MemPath("srv/app/config.ini", backend=backend).exists()
    assert not MemPath("srv/app/cache", backend=backend).exists()


def test_a_plain_copy_keeps_the_backend_path_it_was_built_for():
    backend = MemPathBackend()
    MemPath("srv", backend=backend).mkdir(parents=True)
    host = PosixHost(path_providers=(_memory_provider("mem", backend),))
    original = host.path("srv")

    from hostctl import CompositePosixPath

    copy = CompositePosixPath(original)

    assert copy._backend_path is original._backend_path


def test_a_uri_backed_path_reports_a_filesystem_logical_path():
    """`str()` of an SftpPath is the whole `sftp://host:port/...` URI.

    Used as the logical path it sent I/O to `/sftp:/host:22/...`, so a
    resolved symlink reported `exists()` False and a relative target raised.
    """
    from hostctl import SshConfig

    host = PosixHost.from_ssh(SshConfig("h", username="root"))

    assert str(host.path()) == "/"
    assert str(host.path() / "srv" / "app.conf") == "/srv/app.conf"
    assert str(host.path("/etc/hosts")) == "/etc/hosts"


def test_logical_text_prefers_the_filesystem_spelling():
    from pathlib_next.uri.schemes.sftp import SftpPath

    from hostctl.host.composite_path import _logical_text

    assert (
        _logical_text(SftpPath("sftp://h:22/usr/lib/jvm/java")) == "/usr/lib/jvm/java"
    )
    # A backend with no filesystem spelling keeps its own text.
    assert _logical_text(MemPath("srv/x", backend=MemPathBackend())) == "srv/x"
