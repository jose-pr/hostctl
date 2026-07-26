"""Shared pathlib/path-provider semantics."""

from __future__ import annotations

import os

import pytest

from pathlib_next import Path as NextPath

from .providers import fake_providers, provider_context


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_path_round_trip_and_type(provider, tmp_path):
    if "path" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no path capability")
    with provider_context(provider) as host:
        path = host.path(tmp_path, "payload.txt")
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
        missing = host.path(tmp_path, "missing")
        with pytest.raises(FileNotFoundError):
            missing.read_bytes()
        directory = host.path(tmp_path, "dir")
        directory.mkdir()
        # pathlib on Windows reports EACCES for opening a directory; remote
        # backends normalize this to IsADirectoryError. Both are explicit
        # failures (never a silent read).
        with pytest.raises((IsADirectoryError, PermissionError)):
            directory.read_bytes()
        file_path = host.path(tmp_path, "file")
        file_path.write_bytes(b"x")
        with pytest.raises(NotADirectoryError):
            list(file_path.iterdir())


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_path_empty_text_and_metadata_roundtrip(provider, tmp_path):
    if "path" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no path capability")
    with provider_context(provider) as host:
        empty = host.path(tmp_path, "empty.bin")
        empty.write_bytes(b"")
        assert empty.read_bytes() == b""
        text = host.path(tmp_path, "text.txt")
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
        target = host.path(tmp_path, "missing-target")
        link = host.path(tmp_path, "dangling")
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlink unavailable: {exc}")
        assert link.exists() is False


def test_cross_provider_copy_local_and_container(tmp_path):
    providers = {item.name: item for item in fake_providers()}
    source = tmp_path / "source.bin"
    source.write_bytes(b"cross-provider\n")
    with (
        provider_context(providers["local"]) as local,
        provider_context(providers["container"]) as container,
    ):
        target = container.path(tmp_path, "target.bin")
        local.path(source).copy(target)
        assert target.read_bytes() == source.read_bytes()
        with pytest.raises(FileExistsError):
            local.path(source).copy(target)
