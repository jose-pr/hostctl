"""Shared pathlib/path-provider semantics."""

from __future__ import annotations

import os

import pytest

from pathlib_next import Path as NextPath

from .providers import fake_providers, provider_context


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_path_round_trip_and_type(provider, tmp_path):
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
