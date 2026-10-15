"""`pathapi.protocols.supports()` on a host path agrees with what it does.

The answer is advisory by pathapi's contract, but it must not contradict the
transport: where it says no, the primitive refuses with `NotImplementedError`,
and where it says yes, the primitive works.
"""

from __future__ import annotations

import pytest
from pathapi.protocols import Create, Move, Remove, supports

from .providers import conformance_path, fake_providers, provider_context

_PATH_PROVIDERS = tuple(
    provider for provider in fake_providers() if "path" in provider.capabilities
)


@pytest.mark.parametrize("provider", _PATH_PROVIDERS, ids=lambda p: p.name)
def test_create_support_matches_mkdir(provider, tmp_path):
    with provider_context(provider) as host:
        directory = conformance_path(host, provider, tmp_path, "made")
        if supports(directory, Create):
            directory.mkdir()
            assert directory.is_dir()
        else:
            with pytest.raises(NotImplementedError):
                directory.mkdir()


@pytest.mark.parametrize("provider", _PATH_PROVIDERS, ids=lambda p: p.name)
def test_remove_support_matches_unlink(provider, tmp_path):
    with provider_context(provider) as host:
        target = conformance_path(host, provider, tmp_path, "gone.txt")
        target.write_bytes(b"x")
        if supports(target, Remove):
            target.unlink()
            assert not target.exists()
        else:
            with pytest.raises(NotImplementedError):
                target.unlink()
            assert target.exists()


@pytest.mark.parametrize("provider", _PATH_PROVIDERS, ids=lambda p: p.name)
def test_move_support_matches_rename(provider, tmp_path):
    with provider_context(provider) as host:
        source = conformance_path(host, provider, tmp_path, "before.txt")
        target = conformance_path(host, provider, tmp_path, "after.txt")
        source.write_bytes(b"x")
        if supports(source, Move):
            source.rename(target)
            assert target.read_bytes() == b"x"
        else:
            with pytest.raises(NotImplementedError):
                source.rename(target)
            assert source.exists()
