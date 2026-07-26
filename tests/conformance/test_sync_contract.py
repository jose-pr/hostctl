"""Sync-helper semantics across every transport's production path classes.

These cases complement ``tests/test_sync_helpers.py``, which drives the helpers
with in-memory doubles.  Here the helpers run against the same concrete
``Path``/``Host`` implementations the copy matrix uses, so provider identity,
shell-flavour command construction, and ``PathSyncer`` traversal are exercised
end to end rather than mocked.
"""

from __future__ import annotations

import pytest

from pathlib_next.utils.sync import PathAndStat, PathSyncer, SyncEvent

from hostctl.sync import host_checksum, stat_checksum

from .providers import conformance_path, fake_providers, provider_context

_PATH_PROVIDERS = tuple(
    provider for provider in fake_providers() if "path" in provider.capabilities
)


@pytest.mark.parametrize("provider", _PATH_PROVIDERS, ids=lambda p: p.name)
def test_stat_checksum_reads_no_content(provider, tmp_path):
    """The quick check must answer from the cached stat alone."""

    with provider_context(provider) as host:
        path = conformance_path(host, provider, tmp_path, "stat-checksum.bin")
        path.write_bytes(b"stat checksum payload")
        entry = PathAndStat(path)

        size, mtime = stat_checksum(entry)

        assert size == len(b"stat checksum payload")
        assert mtime == entry.stat.st_mtime
        # A second call must not need the path at all: drop it and re-check.
        assert stat_checksum(PathAndStat.from_stat(path, entry.stat)) == (size, mtime)


@pytest.mark.parametrize("provider", _PATH_PROVIDERS, ids=lambda p: p.name)
def test_stat_checksum_rejects_a_missing_path(provider, tmp_path):
    with provider_context(provider) as host:
        missing = conformance_path(host, provider, tmp_path, "stat-checksum-missing")
        with pytest.raises(FileNotFoundError):
            stat_checksum(PathAndStat(missing))


@pytest.mark.parametrize("provider", _PATH_PROVIDERS, ids=lambda p: p.name)
def test_host_checksum_claims_paths_by_identity_not_by_spelling(provider, tmp_path):
    """Ownership is decided by provider/backend identity, never by prefix.

    Two hosts of the same transport produce identically *spelled* paths, so a
    string comparison would claim both.  Only the owning host may route a path
    to a remote command; a same-looking path from another host must not be
    claimed.
    """

    from hostctl.sync import _host_owns_path, _host_path_token

    if provider.name == "local":
        pytest.skip(
            "two LocalHosts address one filesystem, so both correctly own the path"
        )

    with (
        provider_context(provider) as owner,
        provider_context(provider) as stranger,
    ):
        owned = conformance_path(owner, provider, tmp_path, "identity.bin")
        foreign = conformance_path(stranger, provider, tmp_path, "identity.bin")

        # Identical text, different backing host: a prefix match would tie.
        assert str(owned) == str(foreign)

        token = _host_path_token(owner)
        assert _host_owns_path(owner, owned, token)
        assert not _host_owns_path(owner, foreign, token)


@pytest.mark.parametrize("provider", _PATH_PROVIDERS, ids=lambda p: p.name)
def test_host_checksum_matches_the_local_digest(provider, tmp_path):
    """Whatever route the helper takes, the digest must be the real md5."""

    import hashlib

    payload = b"host checksum fidelity payload"
    with provider_context(provider) as host:
        path = conformance_path(host, provider, tmp_path, "digest.bin")
        path.write_bytes(payload)

        digest = host_checksum(host)(PathAndStat(path))

        assert digest == hashlib.md5(payload).hexdigest()


@pytest.mark.parametrize("provider", _PATH_PROVIDERS, ids=lambda p: p.name)
def test_path_syncer_mirrors_a_tree_over_production_paths(provider, tmp_path):
    """A full ``PathSyncer`` run over production paths, with stat checksums."""

    with provider_context(provider) as host:
        if provider.name == "container":
            pytest.skip("container archive paths cannot create directories")
        source = conformance_path(host, provider, tmp_path, "sync-source")
        target = conformance_path(host, provider, tmp_path, "sync-target")
        source.mkdir(parents=True, exist_ok=True)
        (source / "first.txt").write_bytes(b"first payload")
        (source / "second.txt").write_bytes(b"second payload")

        copied = []

        def hook(src, dst, event, dry_run):
            if event is SyncEvent.Copy:
                copied.append(str(dst))

        PathSyncer(stat_checksum, hook=hook).sync(source, target)

        assert (target / "first.txt").read_bytes() == b"first payload"
        assert (target / "second.txt").read_bytes() == b"second payload"
        assert len(copied) == 2


@pytest.mark.parametrize("provider", _PATH_PROVIDERS, ids=lambda p: p.name)
def test_path_syncer_skips_files_whose_stat_already_matches(provider, tmp_path):
    """The quick check must copy only what differs.

    Note this asserts on files whose stat genuinely matches.  It deliberately
    does *not* re-run a sync and expect a no-op: ``Path.copy()`` preserves
    ``st_mode`` but not timestamps, so a file hostctl just copied has a fresh
    mtime and legitimately compares unequal.  That caveat is documented on
    ``stat_checksum`` and covered by
    ``test_stat_checksum_does_not_converge_after_a_copy`` below.
    """

    with provider_context(provider) as host:
        if provider.name == "container":
            pytest.skip("container archive paths cannot create directories")
        source = conformance_path(host, provider, tmp_path, "skip-source")
        target = conformance_path(host, provider, tmp_path, "skip-target")
        source.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)

        # Build a target entry whose stat matches its source exactly.
        (source / "same.txt").write_bytes(b"identical")
        (target / "same.txt").write_bytes(b"identical")
        matching = stat_checksum(PathAndStat(source / "same.txt")) == stat_checksum(
            PathAndStat(target / "same.txt")
        )
        if not matching:
            pytest.skip(f"{provider.name} cannot produce two stat-identical files")
        (source / "different.txt").write_bytes(b"only in source")

        copied = []

        def hook(src, dst, event, dry_run):
            if event is SyncEvent.Copy:
                copied.append(dst.path.name)

        PathSyncer(stat_checksum, hook=hook).sync(source, target)

        assert copied == ["different.txt"]


@pytest.mark.parametrize("provider", _PATH_PROVIDERS, ids=lambda p: p.name)
def test_stat_checksum_does_not_converge_after_a_copy(provider, tmp_path):
    """Pin the documented caveat so a future upstream fix is noticed.

    ``pathlib_next.Path.copy()`` propagates ``st_mode`` only.  Until it also
    preserves timestamps, a ``stat_checksum`` sync cannot settle into a no-op,
    and the guide says so.  If this ever starts failing, upstream gained
    timestamp preservation and the documentation should be revisited.
    """

    import os

    with provider_context(provider) as host:
        source = conformance_path(host, provider, tmp_path, "converge-source.bin")
        target = conformance_path(host, provider, tmp_path, "converge-target.bin")
        source.write_bytes(b"converge payload")

        # Age the source well beyond any filesystem timestamp granularity, so
        # the comparison below cannot pass by coincidence of a coarse clock.
        aged = stat_checksum(PathAndStat(source))[1] - 3600
        try:
            os.utime(str(source), (aged, aged))
        except (OSError, NotImplementedError):
            pytest.skip(f"{provider.name} cannot set timestamps for this check")
        if stat_checksum(PathAndStat(source))[1] != aged:
            pytest.skip(f"{provider.name} did not honor the backdated timestamp")

        source.copy(target)

        source_stat = stat_checksum(PathAndStat(source))
        target_stat = stat_checksum(PathAndStat(target))

        assert source_stat[0] == target_stat[0]  # size survives the copy
        assert source_stat[1] != target_stat[1]  # the modification time does not


@pytest.mark.parametrize("provider", _PATH_PROVIDERS, ids=lambda p: p.name)
def test_path_syncer_dry_run_leaves_the_target_untouched(provider, tmp_path):
    with provider_context(provider) as host:
        source = conformance_path(host, provider, tmp_path, "dry-run-source.bin")
        target = conformance_path(host, provider, tmp_path, "dry-run-target.bin")
        source.write_bytes(b"proposed content")

        PathSyncer(stat_checksum).sync(source, target, dry_run=True)

        assert not target.exists()
