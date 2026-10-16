"""Invariants of the provider registry itself.

These two lived in `providers.py`, which pytest's default `python_files`
never collects -- so they had never run. What they assert is covered
nowhere else: `Provider.__post_init__` catches a missing `symlink_gap` but
not a provider that advertises `symlink` AND carries one, and nothing else
checks that a fake has not quietly collapsed back to `LocalHost`, which is
the whole premise of the battery.
"""

from __future__ import annotations

from hostctl.transports.local._host import LocalHost

from .providers import contract_providers, deterministic_providers


def test_provider_registry_is_capability_explicit() -> None:
    providers = contract_providers()
    assert {item.name for item in providers} >= {"local", "ssh", "winrm"}
    for provider in providers:
        assert provider.capabilities
        assert callable(provider.factory)
        # A path provider either advertises symlink support or names the
        # transport limitation; an unexplained gap is a registry bug.
        if "path" in provider.capabilities:
            assert ("symlink" in provider.capabilities) != bool(provider.symlink_gap)


def test_transport_fakes_are_not_local_host_aliases() -> None:
    """Registry entries must not silently collapse back to ``LocalHost``."""

    for provider in deterministic_providers():
        value = provider.factory()
        host = value[0] if isinstance(value, tuple) else value
        # The second element is the fake's own cleanup, which is what
        # releases a sandbox directory. Closing only the host left it to the
        # collector -- invisible until the suite made warnings errors.
        cleanup = value[1] if isinstance(value, tuple) and len(value) > 1 else None
        try:
            if provider.name != "local":
                assert not isinstance(host, LocalHost)
                assert type(host) is not LocalHost
        finally:
            close = getattr(host, "close", None)
            if close:
                close()
            if cleanup is not None:
                cleanup()


def test_every_deterministic_leg_runs_the_local_python_cases() -> None:
    """`local-python` gates every case that runs `sys.executable`; a default
    leg without it would skip those silently instead of running them."""
    for provider in deterministic_providers():
        assert "local-python" in provider.capabilities, provider.name


def test_the_live_gates_register_their_legs(monkeypatch) -> None:
    """A gate that is set registers its leg even when the service is down:
    the factory's failure then skips each case with its cause."""
    monkeypatch.setenv("HOSTCTL_TEST_SSH_LOCAL", "1")
    monkeypatch.setenv("HOSTCTL_TEST_DOCKER", "1")
    names = {provider.name for provider in contract_providers()}
    assert {"ssh-local", "docker-live"} <= names
    assert not names & {"loop-serial"}
