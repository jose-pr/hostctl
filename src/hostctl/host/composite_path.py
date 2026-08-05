"""Logical pathlib paths which select and retain host path providers.

The composite classes deliberately inherit the concrete ``pathlib_next``
pathname flavour instead of ``LocalPath``.  This keeps POSIX syntax stable on
Windows clients (and vice versa) while the provider-owned path remains the
authority for I/O.
"""

from __future__ import annotations

import inspect
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


def _accepts_kwargs(
    method: typing.Callable[..., object],
    kwargs: dict[str, object],
    operation: PathOperation,
) -> dict[str, object]:
    """Return ``kwargs``, first checking the backend method accepts them.

    Composite dispatch normalises to the stdlib signature, which made a
    backend's documented extension unreachable through the wrapper.  Rather
    than forward blindly -- which turns a clear ``TypeError`` here into a
    confusing one from inside a transport -- consult the selected backend's
    signature and reject at this boundary what it cannot take.

    A method whose signature cannot be introspected (a C function, a
    ``functools.partial`` over one) is given the benefit of the doubt and
    the kwargs are forwarded; a backend that then rejects them raises its
    own ``TypeError``, which is no worse than calling it directly.
    """

    if not kwargs:
        return kwargs

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return kwargs

    parameters = signature.parameters
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return kwargs

    unsupported = sorted(name for name in kwargs if name not in parameters)
    if unsupported:
        owner = getattr(method, "__self__", None)
        backend = type(owner).__name__ if owner is not None else "backend path"
        raise TypeError(
            f"{backend}.{operation}() does not accept "
            + ", ".join(repr(name) for name in unsupported)
        )
    return kwargs


class _CompositePathMixin:
    """Provider routing shared by the POSIX and Windows concrete classes."""

    __slots__ = ()

    def copy(self, target, **kwargs):
        return Path.copy(self, target, **kwargs)

    def move(self, target, **kwargs):
        return Path.move(self, target, **kwargs)

    def _copy_from(self, source, **kwargs):
        """Accept Python 3.14 stdlib ``Path.copy()`` destinations."""

        if self.exists() and not kwargs.get("overwrite", False):
            raise FileExistsError(str(self))
        with source.open("rb") as src, self.open("wb") as dst:
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)
        return self

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
        return self._selection_trace

    def _adopt(
        self, provider: PathProvider, backend_path: Path, *, pinned: bool
    ) -> None:
        self._provider = provider
        self._backend_path = backend_path
        self._factory = provider.path
        if pinned:
            self._pinned = True

    def _providers_in_order(self, operation: PathOperation, *, pin: bool = False):
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
                    selected = self._selector.select(exclude=excluded, pin=pin)
                except OperationNotStarted:
                    return
                self._selection_trace = selected.trace
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
        callback: typing.Callable[[Path], object],
        *,
        pin: bool = False,
        with_provider: bool = False,
    ):
        candidates = self._providers_in_order(operation, pin=pin)
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
            except OperationNotStarted as exc:
                if pin:
                    self._provider, self._backend_path, self._factory, self._pinned = (
                        old
                    )
                if self._selector is not None:
                    # The provider proved nothing started, so remember the
                    # refusal for this generation instead of re-attempting it
                    # on every later operation.
                    self._selector.decline(provider.name, str(exc))
                continue
            # Reads are intentionally not pinned: the selected backend applies
            # to this operation only and is not retained on the path.
            return (result, provider) if with_provider else result
        if not attempted:
            raise NotImplementedError(f"no path provider supports {operation}")
        raise OperationNotStarted(f"no path provider completed {operation}")

    def via(self, name: str):
        provider = next((item for item in self._providers if item.name == name), None)
        if provider is None:
            raise ValueError(f"unknown path provider: {name}")
        if provider is self._provider and self._pinned:
            return self
        if self._selector is not None:
            probe = provider.probe()
            if not probe.usable:
                raise OperationNotStarted(f"path provider {name!r} is unavailable")
        backend = self._provider_path(provider)
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

    def _adopt_state_from(self, other: "_CompositePathMixin") -> None:
        """Copy provider routing state onto a path built by pure-path code.

        ``pathlib.PurePath`` derivations (``parents``, ``with_name``,
        ``with_suffix``, ``relative_to``) build instances with
        ``object.__new__``, which skips ``__new__`` and therefore leaves every
        composite slot unset.  Re-seeding the routing state here keeps the
        provider collection, selector, and pin attached to the derived path
        instead of raising ``AttributeError`` on the next operation.
        """
        self._provider = other._provider
        self._providers = other._providers
        self._selector = other._selector
        self._factory = other._factory
        self._pinned = other._pinned
        self._selection_trace = other._selection_trace
        # The logical name changed, so the cached backend path no longer
        # describes this path; it is re-derived lazily from the factory.
        self._backend_path = None

    def _provider_path(self, provider: PathProvider) -> Path:
        if (
            provider is self._provider
            and getattr(self, "_backend_path", None) is not None
        ):
            return self._backend_path
        return provider.path(str(self))

    def _derive(self, factory, *args, **kwargs):
        """Run a pure-path derivation and re-attach routing state."""
        derived = factory(*args, **kwargs)
        if isinstance(derived, _CompositePathMixin):
            derived._adopt_state_from(self)
        return derived

    @property
    def parents(self):
        return tuple(
            self._child(str(item)) for item in tuple(super().parents)  # type: ignore[misc]
        )

    def with_name(self, name):
        return self._derive(super().with_name, name)

    def with_stem(self, stem):
        return self._derive(super().with_stem, stem)

    def with_suffix(self, suffix):
        return self._derive(super().with_suffix, suffix)

    def relative_to(self, *other, **kwargs):
        return self._derive(super().relative_to, *other, **kwargs)

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

    def _scan_with_provider(self):
        # The provider's iterator is returned only after the pre-dispatch
        # operation has succeeded; errors after that point are terminal.
        return self._dispatch(
            "scandir", lambda path: iter(path.iterdir()), with_provider=True
        )

    def _scandir(self):
        """Yield ``(name, stat)`` pairs for ``walk()``/``glob()``.

        ``pathlib_next.Path._scandir`` is a listing hook with a fixed shape;
        overriding it with a different return type silently breaks every
        caller.  Provider selection still happens once, in
        ``_scan_with_provider``.
        """
        from pathlib_next.path import FileStat

        for entry in self.iterdir():
            try:
                stat = FileStat.from_path(entry, follow_symlink=False)
            except OSError:
                stat = None
            yield entry.name, stat

    def iterdir(self):
        children, provider = self._scan_with_provider()
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

    def mkdir(self, mode=0o777, parents=False, exist_ok=False, **kwargs):
        return self._dispatch(
            "mkdir",
            lambda path: path.mkdir(
                mode=mode,
                parents=parents,
                exist_ok=exist_ok,
                **_accepts_kwargs(path.mkdir, kwargs, "mkdir"),
            ),
            pin=True,
        )

    def chmod(self, mode, *, follow_symlinks=True, **kwargs):
        return self._dispatch(
            "chmod",
            lambda path: path.chmod(
                mode,
                follow_symlinks=follow_symlinks,
                **_accepts_kwargs(path.chmod, kwargs, "chmod"),
            ),
            pin=True,
        )

    def symlink_to(self, target, target_is_directory: bool = False, **kwargs):
        """Create this path as a symlink, through the selected provider.

        The backend path class owns the transport's real capability: a
        backend without symlink support raises ``NotImplementedError``
        (never a silent no-op), and that surfaces here unchanged.

        Extra keyword arguments are forwarded to the backend's
        ``symlink_to`` only when its signature actually accepts them --
        see :func:`_accepts_kwargs`.  A backend extension (for example
        pytruenas' ``force=``/``onremove=``) is therefore reachable through
        the composite wrapper, while a kwarg no backend understands still
        fails at this boundary with a plain ``TypeError`` rather than an
        obscure one from inside a transport.
        """
        logical_target = str(target)

        def symlink_with_selected_provider(path):
            method = getattr(path, "symlink_to", None)
            if method is None:
                raise NotImplementedError(
                    f"{type(path).__name__} does not support symlink_to"
                )
            extra = _accepts_kwargs(method, kwargs, "symlink_to")
            return method(logical_target, target_is_directory, **extra)

        return self._dispatch("symlink_to", symlink_with_selected_provider, pin=True)

    def readlink(self):
        def readlink_with_selected_provider(path):
            method = getattr(path, "readlink", None)
            if method is None:
                raise NotImplementedError(
                    f"{type(path).__name__} does not support readlink"
                )
            return method()

        target, provider = self._dispatch(
            "readlink", readlink_with_selected_provider, with_provider=True
        )
        # readlink() reports the stored target verbatim -- a relative target
        # stays relative, exactly like pathlib.Path.readlink(). Rebuild it as
        # a composite path so the result keeps this path's provider routing.
        return type(self).from_path(
            provider.path(str(target)),
            provider,
            provider.path,
            self._providers,
            self._selector,
            pinned=self._pinned,
            logical_segments=(str(target),),
        )

    def unlink(self, missing_ok=False, **kwargs):
        return self._dispatch(
            "unlink",
            lambda path: path.unlink(
                missing_ok=missing_ok,
                **_accepts_kwargs(path.unlink, kwargs, "unlink"),
            ),
            pin=True,
        )

    def rmdir(self, **kwargs):
        return self._dispatch(
            "rmdir",
            lambda path: path.rmdir(**_accepts_kwargs(path.rmdir, kwargs, "rmdir")),
            pin=True,
        )

    def rename(self, target):
        logical_target = str(target)

        def rename_with_selected_provider(path):
            provider = self._provider
            if provider is None:  # pragma: no cover - guarded by _dispatch
                raise NotImplementedError("rename requires a path provider")
            if isinstance(target, _CompositePathMixin):
                if target._pinned and target.provider is not provider:
                    raise ValueError("cannot rename across path providers")
                if not any(item is provider for item in target.providers):
                    raise ValueError("cannot rename across path providers")
                target_path = target._provider_path(provider)
            else:
                target_path = provider.path(logical_target)
            path.rename(target_path)
            return type(self).from_path(
                target_path,
                provider,
                provider.path,
                self._providers,
                self._selector,
                pinned=True,
                logical_segments=(logical_target,),
            )

        return self._dispatch("rename", rename_with_selected_provider, pin=True)


class CompositePosixPath(_CompositePathMixin, PosixPathname, Path):
    """A provider-selecting path with POSIX syntax on every client OS."""

    __slots__ = (
        "_provider",
        "_backend_path",
        "_providers",
        "_selector",
        "_factory",
        "_pinned",
        "_selection_trace",
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
        self._selection_trace = (
            inherited._selection_trace if inherited is not None else ()
        )
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
        "_selection_trace",
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
        self._selection_trace = (
            inherited._selection_trace if inherited is not None else ()
        )
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
