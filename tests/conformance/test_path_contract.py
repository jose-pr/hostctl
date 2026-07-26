"""Shared pathlib/path-provider semantics."""

from __future__ import annotations

import os

import pytest

from pathlib_next import Path as NextPath

from .providers import conformance_path, fake_providers, provider_context

_PATH_PROVIDERS = tuple(
    provider for provider in fake_providers() if "path" in provider.capabilities
)


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
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unavailable")
    with provider_context(provider) as host:
        target = conformance_path(host, provider, tmp_path, "missing-target")
        link = conformance_path(host, provider, tmp_path, "dangling")
        try:
            link.symlink_to(target)
        except (AttributeError, OSError, NotImplementedError) as exc:
            pytest.skip(f"symlink unavailable: {exc}")
        assert link.exists() is False


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
