"""Provider/pin retention across every path derivation.

``pathlib.PurePath`` builds several derivations with ``object.__new__``, which
bypasses the composite constructor entirely.  Any derivation that loses the
routing state raises ``AttributeError`` on its next operation, so these cases
guard the whole derivation surface rather than one method.
"""

import pytest
from pathlib_next.mempath import MemPath, MemPathBackend

from hostctl import PathProvider, PosixHost


@pytest.fixture
def host():
    backend = MemPathBackend()
    MemPath("root", backend=backend).mkdir()
    MemPath("root/a.txt", backend=backend).write_bytes(b"a")
    MemPath("root/sub", backend=backend).mkdir()
    MemPath("root/sub/b.txt", backend=backend).write_bytes(b"b")
    primary = PathProvider("primary", lambda *p: MemPath(*p, backend=backend))
    secondary = PathProvider(
        "secondary", lambda *p: MemPath(*p, backend=MemPathBackend())
    )
    return PosixHost(path_providers=(primary, secondary))


@pytest.mark.parametrize(
    "derive",
    [
        pytest.param(lambda p: p.parent, id="parent"),
        pytest.param(lambda p: p.parents[0], id="parents"),
        pytest.param(lambda p: p / "child", id="truediv"),
        pytest.param(lambda p: p.joinpath("child"), id="joinpath"),
        pytest.param(lambda p: p.with_name("other.txt"), id="with_name"),
        pytest.param(lambda p: p.with_suffix(".md"), id="with_suffix"),
        pytest.param(lambda p: p.with_stem("other"), id="with_stem"),
        pytest.param(lambda p: p.with_segments("elsewhere"), id="with_segments"),
    ],
)
def test_every_derivation_retains_the_provider_and_pin(host, derive):
    pinned = host.path("root", "a.txt").via("primary")

    derived = derive(pinned)

    assert derived.provider is pinned.provider
    assert derived._pinned is True
    assert derived.providers == pinned.providers
    # The derived path must remain operable, not merely carry attributes:
    # routing state is resolved lazily, so a lost slot surfaces here.
    assert derived._provider_path(derived.provider) is not None


@pytest.mark.parametrize(
    "derive, expected",
    [
        pytest.param(lambda p: p.parent, True, id="parent"),
        pytest.param(lambda p: p.parents[0], True, id="parents"),
        pytest.param(lambda p: p.with_name("b.txt"), False, id="with_name"),
        pytest.param(lambda p: p.with_suffix(".missing"), False, id="with_suffix"),
    ],
)
def test_derived_paths_still_perform_io(host, derive, expected):
    """A derivation that lost its backend would raise instead of answering."""
    pinned = host.path("root", "a.txt").via("primary")

    assert derive(pinned).exists() is expected


def test_relative_to_retains_routing_state(host):
    pinned = host.path("root", "a.txt").via("primary")

    try:
        derived = pinned.relative_to("root")
    except NotImplementedError:
        # `relative_to` resolves through the underlying pathname class, and
        # MemPath does not implement it on every interpreter. The routing
        # contract below is what this test owns; skip where the backing
        # implementation cannot produce a derived path at all.
        pytest.skip("backing pathname does not implement relative_to here")

    assert str(derived) == "a.txt"
    assert derived.provider is pinned.provider


def test_glob_and_rglob_yield_operable_descendants(host):
    root = host.path("root")

    assert sorted(str(item) for item in root.glob("*")) == ["root/a.txt", "root/sub"]
    assert sorted(str(item) for item in root.rglob("*.txt")) == [
        "root/a.txt",
        "root/sub/b.txt",
    ]


def test_walk_traverses_the_tree_through_the_provider(host):
    root = host.path("root")

    walked = {
        str(top): (sorted(dirs), sorted(files)) for top, dirs, files in root.walk()
    }

    assert walked == {
        "root": (["sub"], ["a.txt"]),
        "root/sub": ([], ["b.txt"]),
    }


def test_glob_and_walk_descendants_preserve_the_pin(host):
    pinned = host.path("root").via("primary")

    globbed = list(pinned.glob("*"))
    walked = [top for top, _, _ in pinned.walk()]

    assert globbed and all(item.provider is pinned.provider for item in globbed)
    assert globbed and all(item._pinned for item in globbed)
    assert walked and all(item.provider is pinned.provider for item in walked)
    assert walked and all(item._pinned for item in walked)


def test_iterdir_children_preserve_the_pin(host):
    pinned = host.path("root").via("primary")

    children = list(pinned.iterdir())

    assert sorted(child.name for child in children) == ["a.txt", "sub"]
    assert all(child.provider is pinned.provider for child in children)
    assert all(child._pinned for child in children)


def test_pinning_does_not_change_the_logical_path_string(host):
    path = host.path("root", "a.txt")

    assert str(path.via("primary")) == str(path)
    assert str(path.via("secondary")) == str(path)
