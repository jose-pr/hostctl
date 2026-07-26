"""Pathlib-compatible logical paths retaining their selected provider."""

from __future__ import annotations

import os
from pathlib_next.fspath import LocalPath


class CompositePath(LocalPath):
    """A lightweight ``pathlib_next.Path`` facade pinned to one provider.

    The provider path remains authoritative for I/O; this class supplies the
    normal pathlib syntax and preserves the pin for descendants.
    """

    __slots__ = ("_provider", "_backend_path", "_factory")

    def __new__(cls, backend_path, provider, factory):
        return super().__new__(cls, os.fspath(backend_path))

    def __init__(self, backend_path, provider, factory):
        super().__init__(os.fspath(backend_path))
        self._provider = provider
        self._backend_path = backend_path
        self._factory = factory

    @classmethod
    def from_path(cls, backend_path, provider, factory):
        return cls(backend_path, provider, factory)

    @property
    def provider(self):
        return self._provider

    def via(self, name):
        if name != self._provider.name:
            raise ValueError(f"path is pinned to provider {self._provider.name!r}")
        return self

    def with_segments(self, *segments):
        return type(self)(self._factory(*segments), self._provider, self._factory)

    def __truediv__(self, key):
        return self.with_segments(*(tuple(self.parts) + (os.fspath(key),)))

    def joinpath(self, *args):
        result = self
        for arg in args:
            result = result / arg
        return result

    def stat(self, *, follow_symlinks=True):
        return self._backend_path.stat(follow_symlinks=follow_symlinks)

    def open(self, *args, **kwargs):
        return self._backend_path.open(*args, **kwargs)

    def read_bytes(self):
        return self._backend_path.read_bytes()

    def write_bytes(self, data):
        return self._backend_path.write_bytes(data)

    def read_text(self, *args, **kwargs):
        return self._backend_path.read_text(*args, **kwargs)

    def write_text(self, data, *args, **kwargs):
        return self._backend_path.write_text(data, *args, **kwargs)

    def exists(self):
        return self._backend_path.exists()

    def is_file(self):
        return self._backend_path.is_file()

    def is_dir(self):
        return self._backend_path.is_dir()

    def iterdir(self):
        for child in self._backend_path.iterdir():
            yield type(self)(child, self._provider, self._factory)
