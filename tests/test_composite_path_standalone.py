"""`CompositePosixPath.over()`: a composite path with no host.

`host.path()` is built by the same constructor, so most cases run both ways
(`build`): whatever a host-built path does, a standalone one does too.
"""

from __future__ import annotations

import copy
import pathlib
import pickle
import socket

import pytest
from pathapi.mempath import MemPath, MemPathBackend
from pathapi.protocols import Create, Stat, supports

from hostctl import CompositePosixPath, CompositeWindowsPath, PosixHost
from hostctl.exceptions import OperationNotStartedError
from hostctl.provider import (
    READ_ONLY_PATH_OPERATIONS,
    PathProvider,
    ProviderProbe,
    ProviderSelector,
)


def _provider(name, backend, *, path_type=MemPath, **options):
    return PathProvider(
        name, lambda *parts: path_type(*parts, backend=backend), **options
    )


def _standalone(providers, *segments, backend=None):
    return CompositePosixPath.over(providers, *segments, backend=backend)


def _host_built(providers, *segments, backend=None):
    return PosixHost(path_providers=providers).path(*segments, backend=backend)


@pytest.fixture(params=[_standalone, _host_built], ids=["over", "host"])
def build(request):
    return request.param


def test_each_operation_selects_a_provider_that_declares_it(build):
    backend = MemPathBackend()
    reader = _provider("reader", backend, capabilities=READ_ONLY_PATH_OPERATIONS)
    writer = _provider("writer", backend)
    path = build((reader, writer), "f.txt")

    path.write_bytes(b"payload")
    written_by = path.provider
    path = build((reader, writer), "f.txt")
    assert path.read_bytes() == b"payload"

    assert type(path) is CompositePosixPath
    assert written_by is writer
    assert path.selection_trace[-1].provider == "reader"


def test_a_provider_declining_construction_falls_through_to_the_next(build):
    backend = MemPathBackend()
    calls = []

    def offline(*parts):
        calls.append(parts)
        raise OperationNotStartedError("offline")

    first = PathProvider("first", offline)
    second = PathProvider("second", offline)
    third = _provider("third", backend)

    path = build((first, second, third), "f.txt")
    path.write_bytes(b"x")

    assert path.provider is third
    assert len(calls) == 2
    assert MemPath("f.txt", backend=backend).read_bytes() == b"x"


class _ConnectingProvider(PathProvider):
    def __init__(self, name, backend, error=None):
        super().__init__(name, lambda *parts: MemPath(*parts, backend=backend))
        self.error = error
        self.connects = 0

    def connect(self):
        self.connects += 1
        if self.error is not None:
            raise self.error


def test_a_connect_refusal_declines_the_provider_for_the_operation(build):
    remote, fallback = MemPathBackend(), MemPathBackend()
    MemPath("f.txt", backend=fallback).write_bytes(b"from fallback")
    first = _ConnectingProvider(
        "first", remote, OperationNotStartedError("connection refused")
    )
    second = _ConnectingProvider("second", fallback)

    path = build((first, second), "f.txt")

    assert path.read_bytes() == b"from fallback"
    assert first.connects == 1
    # Declined for the generation: the next operation does not dial it again.
    assert path.exists() is True
    assert first.connects == 1


def test_not_implemented_falls_through_for_this_call_only(build):
    class NoRead(MemPath):
        def read_bytes(self):
            raise NotImplementedError("no reads here")

    backend = MemPathBackend()
    MemPath("f.txt", backend=backend).write_bytes(b"data")
    first = _provider("first", backend, path_type=NoRead)
    second = _provider("second", backend)
    path = build((first, second), "f.txt")

    assert path.read_bytes() == b"data"
    # The refusal named one operation, so the next still starts at `first`.
    assert path.exists() is True
    assert path.selection_trace[-1].provider == "first"


def test_backend_pins_like_via(build):
    one, two = MemPathBackend(), MemPathBackend()
    first, second = _provider("first", one), _provider("second", two)

    pinned = build((first, second), "f.txt", backend="second")
    pinned.write_bytes(b"pinned")
    child = (pinned.parent / "g.txt").with_suffix(".bin")
    child.write_bytes(b"child")

    assert pinned.provider is second and pinned._pinned
    assert MemPath("f.txt", backend=two).read_bytes() == b"pinned"
    assert MemPath("g.bin", backend=two).read_bytes() == b"child"
    assert not MemPath("f.txt", backend=one).exists()
    via = build((first, second), "f.txt").via("second")
    assert via.read_bytes() == b"pinned"


def test_backend_refusals(build):
    backend = MemPathBackend()
    usable = _provider("usable", backend)
    down = _provider(
        "down", backend, probe=lambda: ProviderProbe("unavailable", reason="down")
    )

    def offline(*parts):
        raise OperationNotStartedError("offline")

    declining = PathProvider("declining", offline)
    providers = (usable, down, declining)

    with pytest.raises(ValueError, match="unknown path provider: nope"):
        build(providers, "f", backend="nope")
    with pytest.raises(OperationNotStartedError, match="'down' is unavailable"):
        build(providers, "f", backend="down")
    # A named backend never falls back to another provider.
    with pytest.raises(OperationNotStartedError, match="offline"):
        build(providers, "f", backend="declining")


def test_supports_answers_from_the_declared_capabilities(build):
    backend = MemPathBackend()
    reader = _provider("reader", backend, capabilities=READ_ONLY_PATH_OPERATIONS)
    writer = _provider("writer", backend)

    read_only = build((reader,), "f")
    both = build((reader, writer), "f")

    assert supports(read_only, Stat) and not supports(read_only, Create)
    assert supports(both, Create)
    assert not supports(both.via("reader"), Create)


def test_the_called_method_is_forwarded_to_the_backend(build):
    calls = []

    class RecordingMemPath(MemPath):
        def rm(self, *args, **kwargs):
            calls.append(str(self))
            return super().rm(*args, **kwargs)

    backend = MemPathBackend()
    MemPath("tree/a", backend=backend).mkdir(parents=True)
    path = build((_provider("only", backend, path_type=RecordingMemPath),), "tree")

    path.rm(recursive=True)

    assert calls == ["tree"]
    assert not MemPath("tree", backend=backend).exists()


@pytest.mark.parametrize(
    "derive",
    [
        pytest.param(lambda p: p / "sub" / "b.txt", id="truediv"),
        pytest.param(lambda p: p.joinpath("a.txt"), id="joinpath"),
        pytest.param(lambda p: (p / "a.txt").parent, id="parent"),
        pytest.param(lambda p: (p / "sub" / "b.txt").parents[1], id="parents"),
        pytest.param(lambda p: (p / "x.txt").with_name("a.txt"), id="with_name"),
        pytest.param(lambda p: (p / "a.md").with_suffix(".txt"), id="with_suffix"),
        pytest.param(lambda p: sorted(p.iterdir())[0], id="iterdir"),
        pytest.param(lambda p: copy.copy(p), id="copy"),
    ],
)
def test_every_derivation_keeps_the_providers_and_the_pin(derive):
    backend = MemPathBackend()
    MemPath("root/sub", backend=backend).mkdir(parents=True)
    MemPath("root/a.txt", backend=backend).write_bytes(b"a")
    MemPath("root/sub/b.txt", backend=backend).write_bytes(b"b")
    other = _provider("other", MemPathBackend())
    mine = _provider("mine", backend)
    root = CompositePosixPath.over((other, mine), "root").via("mine")

    derived = derive(root)

    assert type(derived) is CompositePosixPath
    assert derived.providers is root.providers
    assert derived._selector is root._selector
    assert derived.provider is mine and derived._pinned
    assert derived.exists()


def test_relative_to_keeps_the_providers():
    backend = MemPathBackend()
    path = CompositePosixPath.over((_provider("only", backend),), "root", "sub")

    relative = path.relative_to("root")

    assert str(relative) == "sub"
    assert relative.providers is path.providers


def test_a_standalone_path_copies_but_does_not_pickle():
    path = CompositePosixPath.over((_provider("only", MemPathBackend()),), "f")

    path.write_bytes(b"x")
    duplicate = copy.copy(path)

    assert duplicate.providers is path.providers
    assert duplicate._selector is path._selector
    assert duplicate.read_bytes() == b"x"
    # Refused outright, whether or not the provider's factory would pickle.
    with pytest.raises(TypeError, match="bound to live path providers"):
        pickle.dumps(path)
    with pytest.raises(TypeError, match="bound to live path providers"):
        copy.deepcopy(path)


def test_one_selector_shares_its_declines_across_paths():
    backend = MemPathBackend()
    calls = []

    def offline(*parts):
        calls.append(parts)
        raise OperationNotStartedError("offline")

    selector = ProviderSelector(
        (PathProvider("first", offline), _provider("b", backend))
    )

    a = CompositePosixPath.over(selector, "a")
    b = CompositePosixPath.over(selector, "b")

    assert a.provider.name == b.provider.name == "b"
    assert len(calls) == 1
    assert a.providers is b.providers is selector.providers


def test_paths_over_the_same_providers_are_one_filesystem():
    """Two `over()` calls over one provider list name the same files, so a
    copy of a file onto itself is refused instead of truncating it."""
    backend = MemPathBackend()
    MemPath("app.conf", backend=backend).write_bytes(b"payload")
    provider = _provider("only", backend)

    path = CompositePosixPath.over([provider], "app.conf")
    same = CompositePosixPath.over([provider], "app.conf")

    assert path._same_filesystem(same)
    with pytest.raises(OSError, match="same file"):
        path.copy(same, overwrite=True)
    assert path.read_bytes() == b"payload"


def test_paths_over_different_providers_are_two_filesystems():
    source_backend, target_backend = MemPathBackend(), MemPathBackend()
    MemPath("app.conf", backend=source_backend).write_bytes(b"new")
    MemPath("app.conf", backend=target_backend).write_bytes(b"old")
    source = CompositePosixPath.over([_provider("a", source_backend)], "app.conf")
    target = CompositePosixPath.over([_provider("b", target_backend)], "app.conf")

    assert not source._same_filesystem(target)
    source.copy(target, overwrite=True)

    assert MemPath("app.conf", backend=target_backend).read_bytes() == b"new"


def test_the_windows_twin_keeps_windows_syntax():
    backend = MemPathBackend()
    provider = _provider("only", backend)

    path = CompositeWindowsPath.over((provider,), "C:/data", "f.txt")
    name = CompositeWindowsPath.over((provider,), "f.txt")
    name.write_bytes(b"w")

    assert type(path) is CompositeWindowsPath
    assert str(path) == "C:\\data\\f.txt"
    assert str(path.parent) == "C:\\data"
    assert name.read_bytes() == b"w"


@pytest.mark.parametrize(
    "providers, error",
    [
        pytest.param("/etc", TypeError, id="str"),
        pytest.param(b"/etc", TypeError, id="bytes"),
        pytest.param(
            PathProvider("one", lambda *parts: MemPath(*parts)), TypeError, id="one"
        ),
        pytest.param((), ValueError, id="empty"),
    ],
)
def test_over_refuses_what_is_not_a_provider_list(providers, error):
    with pytest.raises(error, match=r"over\(\)"):
        CompositePosixPath.over(providers, "f")


def test_duplicate_provider_names_are_refused():
    backend = MemPathBackend()

    with pytest.raises(ValueError, match="unique"):
        CompositePosixPath.over((_provider("x", backend), _provider("x", backend)))


def test_the_bare_constructor_points_at_over():
    with pytest.raises(TypeError, match=r"CompositePosixPath\.over\(providers"):
        CompositePosixPath("/etc")


def _closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_sftp_first_then_a_narrower_fallback(tmp_path):
    """The shape of a TrueNAS path: SFTP for everything it can do, a
    middleware-like provider when SFTP is unreachable."""
    pytest.importorskip("asyncssh")
    from pathapi import PosixPathname

    from hostctl.transports.ssh import ssh_providers

    from conformance.inprocess_ssh import shared_login_shell_server

    server = shared_login_shell_server()
    name = f"standalone-{tmp_path.name}.txt"
    fallback_backend = MemPathBackend()
    MemPath("/" + name, backend=fallback_backend).write_bytes(b"from fallback")
    fallback = _provider("ws", fallback_backend, capabilities=READ_ONLY_PATH_OPERATIONS)

    run, sftp = ssh_providers(server.config(pathname=PosixPathname))
    try:
        path = CompositePosixPath.over((sftp, fallback), "/", name)
        path.write_bytes(b"over sftp")

        assert path.provider is sftp
        assert (pathlib.Path(server.sftp_root) / name).read_bytes() == (b"over sftp")
        assert path.read_bytes() == b"over sftp"
    finally:
        run.close()

    run, sftp = ssh_providers(
        server.config(pathname=PosixPathname, port=_closed_port())
    )
    try:
        path = CompositePosixPath.over((sftp, fallback), "/", name)

        assert path.read_bytes() == b"from fallback"
        assert path.selection_trace[-1].provider == "ws"
        with pytest.raises(OperationNotStartedError):
            path.write_bytes(b"nowhere to go")
    finally:
        run.close()
