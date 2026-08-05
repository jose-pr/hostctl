"""Backend-specific keyword arguments survive composite dispatch.

The composite path normalises every operation to the stdlib signature, which
made a backend's documented extension unreachable through the very wrapper
meant to expose it.  Forwarding is signature-aware on purpose: a kwarg the
selected backend declares is passed through, and one it does not is rejected
*here*, so the error names the composite boundary instead of surfacing from
inside a transport.
"""

import inspect

import pytest
from pathlib_next.mempath import MemPath, MemPathBackend

from hostctl import PathProvider, PosixHost


class ExtendedMemPath(MemPath):
    """A backend path with an extension, as real backends do."""

    calls: list[tuple[str, dict]] = []

    # MemPath has no symlink_to to delegate to; recording the call is the
    # whole point here -- what is under test is which kwargs arrive, not
    # whether the memory backend can model a symlink.
    def symlink_to(self, target, target_is_directory=False, *, force=False):
        type(self).calls.append(("symlink_to", {"force": force}))

    def mkdir(self, mode=0o777, parents=False, exist_ok=False, *, owner=None):
        type(self).calls.append(("mkdir", {"owner": owner}))
        return super().mkdir(mode=mode, parents=parents, exist_ok=exist_ok)


@pytest.fixture
def host():
    backend = MemPathBackend()
    ExtendedMemPath("root", backend=backend).mkdir()
    ExtendedMemPath("root/target", backend=backend).write_bytes(b"t")
    # Reset *after* fixture setup: the mkdir above is an ExtendedMemPath call
    # too, and would otherwise read as something the test provoked.
    ExtendedMemPath.calls = []
    # ``symlink_to``/``readlink`` are not in DEFAULT_CAPABILITIES, so a
    # provider that supports them has to say so or dispatch never reaches
    # the backend at all.
    provider = PathProvider(
        "extended",
        lambda *p: ExtendedMemPath(*p, backend=backend),
        capabilities=PathProvider.DEFAULT_CAPABILITIES | {"symlink_to", "readlink"},
    )
    return PosixHost(path_providers=(provider,))


def test_backend_extension_kwarg_reaches_the_backend(host):
    link = host.path("root", "link")

    try:
        link.symlink_to("root/target", force=True)
    except NotImplementedError:
        pytest.skip("backing pathname does not implement symlink_to here")

    assert ("symlink_to", {"force": True}) in ExtendedMemPath.calls


def test_mkdir_extension_kwarg_reaches_the_backend(host):
    host.path("root", "made").mkdir(owner="operator")

    assert ("mkdir", {"owner": "operator"}) in ExtendedMemPath.calls


def test_unknown_kwarg_fails_at_the_composite_boundary(host):
    """Not forwarded blindly: the message names the backend, not a transport."""
    link = host.path("root", "link")

    with pytest.raises(TypeError) as excinfo:
        link.symlink_to("root/target", no_such_option=True)

    assert "no_such_option" in str(excinfo.value)
    assert "ExtendedMemPath.symlink_to()" in str(excinfo.value)
    assert ExtendedMemPath.calls == []


def test_no_kwargs_leaves_the_stdlib_call_untouched(host):
    """The common path must not pay for -- or be changed by -- introspection."""
    host.path("root", "plain").mkdir()

    assert ("mkdir", {"owner": None}) in ExtendedMemPath.calls


def test_force_reaches_a_real_backend_end_to_end(tmp_path):
    """`symlink_to(force=)` over a genuine backend, no fake in the way.

    As of pathlib_next 0.9.0+, `force=` is a generic `Path` extension rather
    than a backend-specific one, so this exercises the whole chain --
    composite wrapper, signature check, real filesystem -- which is the call
    that raised `TypeError` before the passthrough existed.
    """
    from pathlib_next import Path as PnPath

    if "force" not in inspect.signature(PnPath.symlink_to).parameters:
        pytest.skip("installed pathlib_next predates symlink_to(force=)")

    provider = PathProvider(
        "local",
        lambda *p: PnPath(*p),
        capabilities=PathProvider.DEFAULT_CAPABILITIES | {"symlink_to", "readlink"},
    )
    host = PosixHost(path_providers=(provider,))
    target = tmp_path / "target"
    target.write_text("t")
    link = tmp_path / "link"
    link.write_text("occupied")  # force= has to displace this

    host.path(str(link).replace("\\", "/")).symlink_to(
        str(target).replace("\\", "/"), force=True
    )

    assert link.is_symlink()
