"""Logical pathlib paths which select and retain host path providers.

The composite classes deliberately inherit the concrete ``pathlib_next``
pathname flavour instead of ``LocalPath``.  This keeps POSIX syntax stable on
Windows clients (and vice versa) while the provider-owned path remains the
authority for I/O.
"""

from __future__ import annotations

import pathlib
import typing

from pathlib_next import Path, PosixPathname, WindowsPathname

from ..provider import OperationNotStarted, PathProvider, ProviderSelector

PathOperation = str


def _supports(provider: PathProvider, operation: PathOperation) -> bool:
    capabilities = provider.capabilities
    return bool(
        "path" in capabilities
        or operation in capabilities
        or (operation.startswith("open_") and "open" in capabilities)
    )


class _CompositePathMixin:
    """Provider routing shared by the POSIX and Windows concrete classes."""

    __slots__ = ()

    @classmethod
    def from_path(
        cls,
        backend_path: Path,
        provider: PathProvider,
        factory: typing.Callable[..., Path],
        providers: typing.Iterable[PathProvider] = (),
        selector: typing.Optional[ProviderSelector] = None,
        *,
        pinned: bool = False,
        logical_segments: typing.Iterable[object] = (),
    ):
        return cls(
            *(logical_segments or (str(backend_path),)),
            backend_path=backend_path,
            provider=provider,
            factory=factory,
            providers=providers,
            selector=selector,
            pinned=pinned,
        )

    @property
    def provider(self) -> typing.Optional[PathProvider]:
        return self._provider

    @property
    def providers(self) -> tuple[PathProvider, ...]:
        return self._providers

    @property
    def selection_trace(self):
        if self._selector is None or self._selector.last_selection is None:
            return ()
        return self._selector.last_selection.trace

    def _provider_path(self, provider: PathProvider) -> Path:
        if provider is self._provider and self._backend_path is not None:
            return self._backend_path
        return provider.path(str(self))

    def _adopt(
        self, provider: PathProvider, backend_path: Path, *, pinned: bool
    ) -> None:
        self._provider = provider
        self._backend_path = backend_path
        self._factory = provider.path
        if pinned:
            self._pinned = True

    def _providers_in_order(self, operation: PathOperation):
        if self._pinned:
            if self._provider is None or not _supports(self._provider, operation):
                raise NotImplementedError(
                    f"provider {getattr(self._provider, 'name', '<none>')!r} "
                    f"does not support {operation}"
                )
            yield self._provider
            return

        if self._selector is not None:
            excluded: list[str] = []
            while True:
                try:
                    # ProviderSelector's capability filter is transport
                    # agnostic; ``path`` is a wildcard only for this path
                    # operation layer, so select in order and apply the
                    # operation check locally.
                    selected = self._selector.select(exclude=excluded)
                except OperationNotStarted:
                    return
                provider = selected.provider
                if _supports(provider, operation):
                    excluded.append(provider.name)
                    yield provider
                else:
                    excluded.append(provider.name)
            return

        for provider in self._providers:
            if _supports(provider, operation):
                try:
                    if provider.probe().usable:
                        yield provider
                except Exception:
                    continue

    def _dispatch(
        self,
        operation: PathOperation,
        callback: typing.Callable[[Path], typing.Any],
        *,
        pin: bool = False,
        with_provider: bool = False,
    ):
        candidates = self._providers_in_order(operation)
        attempted = False
        for provider in candidates:
            attempted = True
            old = (self._provider, self._backend_path, self._factory, self._pinned)
            try:
                backend_path = self._provider_path(provider)
                if pin:
                    # A write/open operation owns the provider choice before
                    # dispatch.  OperationNotStarted is the only safe way to
                    # undo this and try the next provider.
                    self._adopt(provider, backend_path, pinned=True)
                result = callback(backend_path)
            except OperationNotStarted:
                if pin:
                    self._provider, self._backend_path, self._factory, self._pinned = (
                        old
                    )
                continue
            if not pin and provider is not self._provider:
                # Reads are intentionally not pinned, but retain the selected
                # backend only for this operation.
                pass
            return (result, provider) if with_provider else result
        if not attempted:
            raise NotImplementedError(f"no path provider supports {operation}")
        raise OperationNotStarted(f"no path provider completed {operation}")

    def via(self, name: str):
        provider = next((item for item in self._providers if item.name == name), None)
        if provider is None:
            raise ValueError(f"unknown path provider: {name}")
        if provider is self._provider:
            return self
        if self._selector is not None:
            probe = provider.probe()
            if not probe.usable:
                raise OperationNotStarted(f"path provider {name!r} is unavailable")
        backend = provider.path(str(self))
        return type(self).from_path(
            backend,
            provider,
            provider.path,
            self._providers,
            self._selector,
            pinned=True,
            logical_segments=(str(self),),
        )

    def _child(self, *segments: str):
        backend = self._factory(*segments)
        return type(self).from_path(
            backend,
            self._provider,
            self._factory,
            self._providers,
            self._selector,
            pinned=self._pinned,
            logical_segments=segments,
        )

    def with_segments(self, *segments: str):
        return self._child(*segments)

    def __truediv__(self, key):
        return self._child(str(self), str(key))

    def joinpath(self, *args):
        return self._child(str(self), *(str(arg) for arg in args))

    @property
    def parent(self):
        return self._child(str(super().parent))

    def stat(self, *, follow_symlinks: bool = True):
        return self._dispatch(
            "stat", lambda path: path.stat(follow_symlinks=follow_symlinks)
        )

    def _scandir(self):
        # The provider's iterator is returned only after the pre-dispatch
        # operation has succeeded; errors after that point are terminal.
        return self._dispatch(
            "scandir", lambda path: iter(path.iterdir()), with_provider=True
        )

    def iterdir(self):
        children, provider = self._scandir()
        for child in children:
            yield type(self).from_path(
                child,
                provider,
                provider.path,
                self._providers,
                self._selector,
                pinned=self._pinned,
                logical_segments=(str(self), getattr(child, "name", str(child))),
            )

    def open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        raw_mode = mode.replace("b", "")
        write = any(flag in raw_mode for flag in "wax+")
        operation = "open_write" if write else "open_read"
        return self._dispatch(
            operation,
            lambda path: path.open(
                mode,
                buffering=buffering,
                encoding=encoding,
                errors=errors,
                newline=newline,
            ),
            pin=True,
        )

    def read_bytes(self):
        return self._dispatch("read", lambda path: path.read_bytes())

    def write_bytes(self, data):
        return self._dispatch("write", lambda path: path.write_bytes(data), pin=True)

    def read_text(self, *args, **kwargs):
        return self._dispatch("read", lambda path: path.read_text(*args, **kwargs))

    def write_text(self, data, *args, **kwargs):
        return self._dispatch(
            "write", lambda path: path.write_text(data, *args, **kwargs), pin=True
        )

    def exists(self, *, follow_symlinks=True):
        return self._dispatch(
            "exists", lambda path: path.exists(follow_symlinks=follow_symlinks)
        )

    def is_file(self):
        return self._dispatch("is_file", lambda path: path.is_file())

    def is_dir(self):
        return self._dispatch("is_dir", lambda path: path.is_dir())

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        return self._dispatch(
            "mkdir",
            lambda path: path.mkdir(mode=mode, parents=parents, exist_ok=exist_ok),
            pin=True,
        )

    def chmod(self, mode, *, follow_symlinks=True):
        return self._dispatch(
            "chmod",
            lambda path: path.chmod(mode, follow_symlinks=follow_symlinks),
            pin=True,
        )

    def unlink(self, missing_ok=False):
        return self._dispatch(
            "unlink", lambda path: path.unlink(missing_ok=missing_ok), pin=True
        )

    def rmdir(self):
        return self._dispatch("rmdir", lambda path: path.rmdir(), pin=True)

    def rename(self, target):
        if isinstance(target, _CompositePathMixin):
            if target.provider is not self.provider:
                raise ValueError("cannot rename across path providers")
            target_path = target._backend_path
        else:
            target_path = self._factory(str(target))
        return self._dispatch("rename", lambda path: path.rename(target_path), pin=True)


class CompositePosixPath(_CompositePathMixin, PosixPathname, Path):
    """A provider-selecting path with POSIX syntax on every client OS."""

    __slots__ = (
        "_provider",
        "_backend_path",
        "_providers",
        "_selector",
        "_factory",
        "_pinned",
    )

    def __new__(
        cls,
        *segments,
        backend_path=None,
        provider=None,
        factory=None,
        providers=(),
        selector=None,
        pinned=False,
    ):
        inherited = next(
            (
                segment
                for segment in segments
                if isinstance(segment, _CompositePathMixin)
            ),
            None,
        )
        if inherited is not None:
            provider = provider or inherited.provider
            factory = factory or inherited._factory
            providers = providers or inherited.providers
            selector = selector or inherited._selector
            pinned = pinned or inherited._pinned
            backend_path = backend_path or inherited._backend_path
        self = super().__new__(cls, *segments)
        self._provider = provider
        self._backend_path = backend_path
        self._providers = tuple(providers) or ((provider,) if provider else ())
        self._selector = selector
        self._factory = factory or (provider.path if provider else None)
        self._pinned = pinned
        if (
            self._provider is None
            or self._backend_path is None
            or self._factory is None
        ):
            raise TypeError("CompositePosixPath requires a provider-backed path")
        return self

    def __init__(self, *segments, **kwargs):
        if not hasattr(self, "_raw_paths") and not hasattr(self, "_parts"):
            pathlib.PurePath.__init__(self, *segments)


class CompositeWindowsPath(_CompositePathMixin, WindowsPathname, Path):
    """A provider-selecting path with Windows syntax on every client OS."""

    __slots__ = (
        "_provider",
        "_backend_path",
        "_providers",
        "_selector",
        "_factory",
        "_pinned",
    )

    def __new__(
        cls,
        *segments,
        backend_path=None,
        provider=None,
        factory=None,
        providers=(),
        selector=None,
        pinned=False,
    ):
        inherited = next(
            (
                segment
                for segment in segments
                if isinstance(segment, _CompositePathMixin)
            ),
            None,
        )
        if inherited is not None:
            provider = provider or inherited.provider
            factory = factory or inherited._factory
            providers = providers or inherited.providers
            selector = selector or inherited._selector
            pinned = pinned or inherited._pinned
            backend_path = backend_path or inherited._backend_path
        self = super().__new__(cls, *segments)
        self._provider = provider
        self._backend_path = backend_path
        self._providers = tuple(providers) or ((provider,) if provider else ())
        self._selector = selector
        self._factory = factory or (provider.path if provider else None)
        self._pinned = pinned
        if (
            self._provider is None
            or self._backend_path is None
            or self._factory is None
        ):
            raise TypeError("CompositeWindowsPath requires a provider-backed path")
        return self

    def __init__(self, *segments, **kwargs):
        if not hasattr(self, "_raw_paths") and not hasattr(self, "_parts"):
            pathlib.PurePath.__init__(self, *segments)
