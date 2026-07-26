"""Shared pathlib/path-provider semantics."""

from __future__ import annotations

import os
import stat

import pytest

from pathlib_next import Path as NextPath

from .providers import conformance_path, fake_providers, provider_context

_PATH_PROVIDERS = tuple(
    provider for provider in fake_providers() if "path" in provider.capabilities
)


def _link_target_parts(path) -> tuple:
    """Return a backend-independent identity for a symlink target.

    ``readlink()`` reports the target the transport stored, and the exact
    spelling is legitimately backend-specific: Windows ``os.readlink`` adds
    the ``\\\\?\\`` extended-length prefix, and ``SftpPath`` renders as a
    ``sftp://host:port/...`` URI.  Comparing the trailing path components
    checks the contract that actually matters -- the same file is named --
    without asserting one backend's spelling onto the others.
    """

    text = str(path).replace("\\", "/")
    if text.startswith("//?/"):
        text = text[4:]
    # SftpPath renders as "sftp:/host:port/..." (one slash), a Uri as
    # "scheme://host/...". Strip whichever scheme prefix is present.
    for separator in ("://", ":/"):
        if separator in text:
            text = text.split(separator, 1)[1]
            break
    parts = [part for part in text.split("/") if part and part != "."]
    # Drop a leading drive letter or "host:port" authority.
    if parts and ":" in parts[0]:
        parts = parts[1:]
    return tuple(parts)


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_path_round_trip_and_type(provider, tmp_path):
    if "path" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no path capability")
    with provider_context(provider) as host:
        path = conformance_path(host, provider, tmp_path, "payload.txt")
        assert isinstance(path, NextPath)
        path.write_bytes("héllo".encode())
        assert path.read_bytes() == "héllo".encode()
        assert path.exists()
        assert path.is_file()
        assert path.stat().st_size > 0


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_path_error_types(provider, tmp_path):
    if "path" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no path capability")
    with provider_context(provider) as host:
        missing = conformance_path(host, provider, tmp_path, "missing")
        with pytest.raises(FileNotFoundError):
            missing.read_bytes()
        directory = conformance_path(host, provider, tmp_path, "dir")
        if provider.name == "container":
            # Docker's archive API can inspect but cannot create directories.
            (tmp_path / "dir").mkdir()
        else:
            directory.mkdir()
        # pathlib on Windows reports EACCES for opening a directory; remote
        # backends normalize this to IsADirectoryError. Both are explicit
        # failures (never a silent read).
        with pytest.raises((IsADirectoryError, PermissionError)):
            directory.read_bytes()
        file_path = conformance_path(host, provider, tmp_path, "file")
        file_path.write_bytes(b"x")
        with pytest.raises(NotADirectoryError):
            list(file_path.iterdir())


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_path_empty_text_and_metadata_roundtrip(provider, tmp_path):
    if "path" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no path capability")
    with provider_context(provider) as host:
        empty = conformance_path(host, provider, tmp_path, "empty.bin")
        empty.write_bytes(b"")
        assert empty.read_bytes() == b""
        text = conformance_path(host, provider, tmp_path, "text.txt")
        text.write_text("héllo", encoding="utf-8")
        assert text.read_text(encoding="utf-8") == "héllo"
        assert text.stat().st_size == len("héllo".encode())


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_path_dangling_symlink_exists_is_boolean(provider, tmp_path):
    if "path" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no path capability")
    if "symlink" not in provider.capabilities:
        # The provider registry names the real transport limitation; a
        # backend that advertises symlink support must actually deliver it,
        # so no exception below is converted into a skip.
        pytest.skip(f"{provider.name} cannot create symlinks: {provider.symlink_gap}")
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unavailable on this client")
    with provider_context(provider) as host:
        target = conformance_path(host, provider, tmp_path, "missing-target")
        link = conformance_path(host, provider, tmp_path, "dangling")
        try:
            link.symlink_to(target)
        except PermissionError as exc:
            # Windows refuses symlink creation without elevation or
            # Developer Mode.  That is a host policy, not a transport gap,
            # and every backend must report it as PermissionError.
            pytest.skip(f"{provider.name} symlink requires elevation: {exc}")
        assert link.exists() is False
        # is_symlink() -- not exists(follow_symlinks=False) -- is the portable
        # probe here: LocalPath inherits stdlib pathlib.Path.exists(), which
        # only grew the follow_symlinks keyword in 3.12, so the composite and
        # local backends disagree on that signature at the 3.9 floor.
        assert link.is_symlink() is True


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_path_symlink_round_trips_through_readlink(provider, tmp_path):
    """A backend advertising symlinks must read back the stored target."""

    if "path" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no path capability")
    if "symlink" not in provider.capabilities:
        pytest.skip(f"{provider.name} cannot create symlinks: {provider.symlink_gap}")
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unavailable on this client")
    with provider_context(provider) as host:
        target = conformance_path(host, provider, tmp_path, "readlink-target")
        target.write_bytes(b"symlink payload")
        link = conformance_path(host, provider, tmp_path, "readlink-link")
        try:
            link.symlink_to(target)
        except PermissionError as exc:
            pytest.skip(f"{provider.name} symlink requires elevation: {exc}")

        assert link.is_symlink() is True
        # stat() follows by default and lstat() must not, or is_symlink()
        # and exists() would disagree with each other.
        assert link.stat().st_size == len(b"symlink payload")
        assert stat.S_ISLNK(link.stat(follow_symlinks=False).st_mode)
        assert link.read_bytes() == b"symlink payload"
        assert _link_target_parts(link.readlink()) == _link_target_parts(target)


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_path_symlink_gap_is_explicit(provider, tmp_path):
    """A backend without symlink support must say so, never fail silently."""

    if "path" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no path capability")
    if "symlink" in provider.capabilities:
        pytest.skip(f"{provider.name} supports symlinks")
    with provider_context(provider) as host:
        link = conformance_path(host, provider, tmp_path, "unsupported-link")
        with pytest.raises(NotImplementedError):
            link.symlink_to(conformance_path(host, provider, tmp_path, "target"))
        with pytest.raises(NotImplementedError):
            link.readlink()


@pytest.mark.parametrize("provider", _PATH_PROVIDERS, ids=lambda p: p.name)
def test_path_contract_uses_real_transport_backend(provider, tmp_path):
    if "path" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no path capability")
    expected = {
        "local": ("LocalPath",),
        "ssh": ("SftpPath",),
        "winrm": ("WinRMPath",),
        "container": ("PosixContainerPath", "WindowsContainerPath"),
        "qemu": ("PosixQemuPath", "WindowsQemuPath"),
    }
    with provider_context(provider) as host:
        path = conformance_path(host, provider, tmp_path, "identity")
        backend_path = getattr(path, "_backend_path", path)
        assert type(backend_path).__name__ in expected[provider.name]


@pytest.mark.parametrize("source_provider", _PATH_PROVIDERS, ids=lambda p: p.name)
@pytest.mark.parametrize("target_provider", _PATH_PROVIDERS, ids=lambda p: p.name)
def test_cross_backend_copy_matrix(source_provider, target_provider, tmp_path):
    payload = (
        "cross-provider %s → %s\n"
        % (
            source_provider.name,
            target_provider.name,
        )
    ).encode("utf-8")
    with (
        provider_context(source_provider) as source_host,
        provider_context(target_provider) as target_host,
    ):
        source = conformance_path(
            source_host,
            source_provider,
            tmp_path,
            "source-%s-%s.bin" % (source_provider.name, target_provider.name),
        )
        source.write_bytes(payload)
        target = conformance_path(
            target_host,
            target_provider,
            tmp_path,
            "target-%s-%s.bin" % (source_provider.name, target_provider.name),
        )

        NextPath.copy(source, target)

        assert target.read_bytes() == payload
        with pytest.raises(FileExistsError):
            NextPath.copy(source, target)


@pytest.mark.parametrize(
    "target_provider",
    tuple(provider for provider in _PATH_PROVIDERS if provider.name != "local"),
    ids=lambda provider: provider.name,
)
def test_local_path_copy_method_accepts_remote_destination(target_provider, tmp_path):
    """Cover Python 3.14's stdlib-first LocalPath.copy() MRO explicitly."""

    local_provider = next(
        provider for provider in _PATH_PROVIDERS if provider.name == "local"
    )
    with (
        provider_context(local_provider) as local_host,
        provider_context(target_provider) as target_host,
    ):
        source = conformance_path(
            local_host, local_provider, tmp_path, "local-copy-source.bin"
        )
        source.write_bytes(b"stdlib copy destination protocol")
        target = conformance_path(
            target_host,
            target_provider,
            tmp_path,
            "local-copy-target.bin",
        )

        source.copy(target)

        assert target.read_bytes() == source.read_bytes()
