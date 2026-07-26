"""Pathlib-compatible logical paths retaining their selected provider."""

from __future__ import annotations

from pathlib import PurePath

from pathlib_next.fspath import LocalPath


class CompositePath(LocalPath):
    """A lightweight ``pathlib_next.Path`` facade pinned to one provider.

    The provider path remains authoritative for I/O; this class supplies the
    normal pathlib syntax and preserves the pin for descendants.
    """

    __slots__ = ("_provider", "_backend_path", "_factory", "_providers")

    def __new__(cls, backend_path, provider, factory, providers=()):
        return super().__new__(cls, str(backend_path))

    def __init__(self, backend_path, provider, factory, providers=()):
        if PurePath.__init__ is not object.__init__:
            super().__init__(str(backend_path))
        self._provider = provider
        self._backend_path = backend_path
        self._factory = factory
        self._providers = tuple(providers) or (provider,)

    @classmethod
    def from_path(cls, backend_path, provider, factory, providers=()):
        return cls(backend_path, provider, factory, providers)

    @property
    def provider(self):
        return self._provider

    def via(self, name):
        selected = next(
            (provider for provider in self._providers if provider.name == name), None
        )
        if selected is None:
            raise ValueError(f"unknown path provider: {name}")
        value = selected.path(*self.parts)
        return type(self)(value, selected, selected.path, self._providers)

    def with_segments(self, *segments):
        return type(self)(
            self._factory(*segments),
            self._provider,
            self._factory,
            self._providers,
        )

    def __truediv__(self, key):
        return self.with_segments(*(tuple(self.parts) + (str(key),)))

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
            yield type(self)(child, self._provider, self._factory, self._providers)
