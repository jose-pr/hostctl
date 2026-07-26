"""Pathlib-compatible logical paths retaining their selected provider."""

from __future__ import annotations

from pathlib import PurePath, PurePosixPath, PureWindowsPath

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
        if selected is self._provider:
            return self
        value = selected.path(*self.parts)
        return type(self)(value, selected, selected.path, self._providers)

    def with_segments(self, *segments):
        return type(self)(
            self._factory(*segments),
            self._provider,
            self._factory,
            self._providers,
        )

    def _operation_path(self, operation):
        if (
            operation in self._provider.capabilities
            or "path" in self._provider.capabilities
        ):
            return self._backend_path
        for provider in self._providers:
            if operation in provider.capabilities:
                return provider.path(*self.parts)
        raise NotImplementedError(
            f"provider {self._provider.name!r} does not support {operation}"
        )

    def __truediv__(self, key):
        return self.with_segments(*(tuple(self.parts) + (str(key),)))

    def joinpath(self, *args):
        result = self
        for arg in args:
            result = result / arg
        return result

    def stat(self, *, follow_symlinks=True):
        return self._operation_path("stat").stat(follow_symlinks=follow_symlinks)

    def open(self, *args, **kwargs):
        return self._operation_path("open").open(*args, **kwargs)

    def read_bytes(self):
        return self._operation_path("read").read_bytes()

    def write_bytes(self, data):
        return self._operation_path("write").write_bytes(data)

    def read_text(self, *args, **kwargs):
        return self._operation_path("read").read_text(*args, **kwargs)

    def write_text(self, data, *args, **kwargs):
        return self._operation_path("write").write_text(data, *args, **kwargs)

    def exists(self):
        return self._operation_path("exists").exists()

    def is_file(self):
        return self._operation_path("stat").is_file()

    def is_dir(self):
        return self._operation_path("stat").is_dir()

    def iterdir(self):
        for child in self._operation_path("scandir").iterdir():
            yield type(self)(child, self._provider, self._factory, self._providers)


class CompositePosixPath(CompositePath):
    """Composite path with POSIX syntax independent of the client OS."""

    __slots__ = ()

    def __str__(self):
        return str(PurePosixPath(str(self._backend_path).replace("\\", "/")))

    @property
    def parts(self):
        return PurePosixPath(str(self._backend_path).replace("\\", "/")).parts


class CompositeWindowsPath(CompositePath):
    """Composite path with Windows syntax independent of the client OS."""

    __slots__ = ()

    def __str__(self):
        return str(PureWindowsPath(str(self._backend_path).replace("/", "\\")))

    @property
    def parts(self):
        return PureWindowsPath(str(self._backend_path)).parts
