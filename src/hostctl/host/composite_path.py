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


def _is_composite_owner(base: type) -> bool:
    """True for classes belonging to this module's composite hierarchy.

    Used to tell a deliberate composite override (``iterdir``, ``rename``,
    ``readlink``, ``copy``) apart from an inherited ``pathlib_next``
    implementation, which is exactly what forwarding must displace.
    """
    return getattr(base, "__module__", "") == __name__


def _make_forwarder(
    name: str, capability: PathOperation, pin: bool, retry_safe: bool
) -> typing.Callable[..., object]:
    """Build a method forwarding ``name`` to the selected backend path."""

    def forwarder(self, *args, **kwargs):
        # A composite path used as an argument (a symlink target, a
        # samefile operand) is meaningless to the backend, which would
        # re-parse it through its own constructor.  Hand over the logical
        # string and let the backend build its own path type from it.
        args = tuple(
            str(arg) if isinstance(arg, _CompositePathMixin) else arg for arg in args
        )

        def call(path: Path):
            method = getattr(path, name, None)
            if method is None:
                raise NotImplementedError(
                    f"{type(path).__name__} does not support {name}"
                )
            return method(*args, **_accepts_kwargs(method, kwargs, name))

        return self._dispatch(
            capability, call, pin=pin, retry_on_not_implemented=retry_safe
        )

    forwarder.__name__ = name
    forwarder.__qualname__ = f"_CompositePathMixin.{name}"
    forwarder.__doc__ = (
        f"Route ``{name}`` to the selected path provider's backend path.\n\n"
        f"        Forwarded verbatim, so a backend overriding ``{name}`` for a\n"
        f"        transport-native implementation is the code that runs.\n"
        f"        Generated from ``_FORWARDED``; see that table for the\n"
        f"        capability gate and retry contract.\n        "
    )
    return forwarder


# Methods forwarded verbatim to the selected backend path.  Each entry maps a
# method name to the capability string gating it, whether the call pins the
# provider, and whether a `NotImplementedError` from it may fall through to the
# next provider.
#
# The method that was *called* is the method invoked on the backend -- never a
# decomposition into primitives.  Backends override derived operations for real
# optimization (``SftpPath.copy`` fans out over asyncssh workers,
# ``SftpPath.rm``/``checksum`` run server-side, ``LocalPath`` reaches ``shutil``
# and ``os.scandir``), and decomposing would silently discard all of it while
# still producing correct results.  Anything the backend does not override
# resolves to ``pathlib_next``'s own wrapper, so operations added upstream work
# by adding a row here rather than writing a body.
#
# ``retry_safe`` is deliberately conservative.  A wrapper composed of several
# primitives may already have mutated when a later primitive raises --
# ``Path.symlink_to(force=True)`` unlinks *before* calling ``_symlink_to``, so a
# backend lacking that primitive deletes the entry and only then raises.
# ``NotImplementedError`` cannot distinguish "did nothing" from "did half", so
# only calls that cannot mutate before raising opt in.
_FORWARDED: "dict[str, tuple[str, bool, bool]]" = {
    # name: (capability, pin, retry_safe)
    "exists": ("exists", False, True),
    "is_file": ("is_file", False, True),
    "is_dir": ("is_dir", False, True),
    "is_symlink": ("stat", False, True),
    "is_block_device": ("stat", False, True),
    "is_char_device": ("stat", False, True),
    "is_fifo": ("stat", False, True),
    "is_socket": ("stat", False, True),
    "stat": ("stat", False, True),
    "lstat": ("stat", False, True),
    "samefile": ("stat", False, True),
    "read_bytes": ("read", False, True),
    "read_text": ("read", False, True),
    "checksum": ("read", False, True),
    "supported_checksums": ("read", False, True),
    "chown": ("chmod", True, True),
    "chmod": ("chmod", True, True),
    "lchmod": ("chmod", True, True),
    "write_bytes": ("write", True, False),
    "write_text": ("write", True, False),
    "mkdir": ("mkdir", True, False),
    "touch": ("write", True, False),
    "unlink": ("unlink", True, False),
    "rmdir": ("rmdir", True, False),
    "rm": ("unlink", True, False),
    "symlink_to": ("symlink_to", True, False),
}


class _CompositePathMixin:
    """Provider routing shared by the POSIX and Windows concrete classes."""

    __slots__ = ()

    def __init_subclass__(cls, **kwargs):
        """Install the forwarders on every concrete composite class.

        Generated here rather than written out so that following
        ``pathlib_next`` is a row in ``_FORWARDED``, not a new method body.
        A class defining the name in its own body always wins -- that is the
        opt-out for an operation needing real composite logic (``iterdir``,
        ``rename``, ``readlink``, ``copy``/``move``).
        """
        super().__init_subclass__(**kwargs)
        for name, (capability, pin, retry_safe) in _FORWARDED.items():
            # Only a definition inside the composite classes themselves opts
            # out.  Testing the whole MRO would match everything inherited
            # from ``pathlib_next.Path`` -- which is the entire surface this
            # table exists to route.
            owner = next(
                (
                    base
                    for base in cls.__mro__
                    if name in vars(base) and _is_composite_owner(base)
                ),
                None,
            )
            if owner is not None:
                continue
            setattr(cls, name, _make_forwarder(name, capability, pin, retry_safe))

    def copy(self, target, **kwargs):
        return self._transfer("copy", target, **kwargs)

    def move(self, target, **kwargs):
        return self._transfer("move", target, **kwargs)

    def _transfer(self, name, target, **kwargs):
        """Route ``copy``/``move`` to the backend when both ends agree.

        A backend overrides these for transport-native transfer --
        ``SftpPath.copy`` fans out over asyncssh workers rather than
        streaming bytes through the client -- so handing the call straight
        to ``Path.copy`` would be correct and much slower.

        The backend can only be used when the destination resolves to a
        path *it* understands: a plain backend path, or a composite path
        sharing this provider.  Anything else (a composite path on another
        provider, a foreign ``Path``) is a genuine cross-backend transfer,
        which is what the generic implementation exists for.
        """
        generic = getattr(Path, name)
        provider = self._provider
        backend_target = target
        if isinstance(target, _CompositePathMixin):
            if provider is None or not any(
                item is provider for item in target.providers
            ):
                return generic(self, target, **kwargs)
            backend_target = target._provider_path(provider)
        elif isinstance(target, str):
            backend_target = target
        elif not isinstance(target, Path):
            return generic(self, target, **kwargs)

        def call(path: Path):
            method = getattr(type(path), name, None)
            if method is None or method is generic:
                # The backend adds nothing over the generic implementation;
                # use it directly so composite-aware behavior is preserved.
                return generic(self, target, **kwargs)
            return method(path, backend_target, **kwargs)

        # Gated on "write", not a "copy"/"move" capability: neither is in
        # PathProvider.DEFAULT_CAPABILITIES, so gating on the method name
        # would reject every provider that has not opted in by hand.
        return self._dispatch("write", call, pin=True)

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
        retry_on_not_implemented: bool = False,
    ):
        candidates = self._providers_in_order(operation, pin=pin)
        attempted = False
        not_implemented: typing.Optional[NotImplementedError] = None
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
            except NotImplementedError as exc:
                if not retry_on_not_implemented:
                    # The call may have mutated before raising -- a wrapper
                    # composed of primitives can fail partway (see _FORWARDED).
                    # Report it rather than silently repeating the work
                    # against another provider.
                    raise
                not_implemented = exc
                if pin:
                    self._provider, self._backend_path, self._factory, self._pinned = (
                        old
                    )
                if self._selector is not None:
                    self._selector.decline(provider.name, str(exc))
                continue
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
        if not_implemented is not None:
            # Every candidate declined by saying it cannot do this at all.
            # Surfacing OperationNotStarted here would rename a permanent
            # "no backend implements this" into a transient "nothing started",
            # which reads as retryable and hides the real cause -- most
            # visibly with a single provider, where "try the next one" has
            # nothing to try.
            raise not_implemented
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

    def _scandir(self):
        """Yield ``(name, stat)`` pairs, routed to the backend's listing.

        ``_scandir`` is the primitive and ``iterdir`` derives from it, not
        the other way round: a backend whose listing call already carries
        metadata answers in one round trip (``SftpPath`` uses
        ``listdir_attr``; FTP/HTTP/S3 do the equivalent).  Listing via
        ``iterdir`` instead would rebuild every child as a composite path
        and then stat each one separately, discarding that.
        """
        entries, _provider = self._dispatch(
            "scandir",
            lambda path: iter(path._scandir()),
            with_provider=True,
            retry_on_not_implemented=True,
        )
        return entries

    def iterdir(self):
        # Dispatches for the provider as well as the entries: children must
        # be built against the provider that actually scanned, and
        # ``__slots__`` leaves nowhere to stash it between calls.
        entries, provider = self._dispatch(
            "scandir",
            lambda path: iter(path._scandir()),
            with_provider=True,
            retry_on_not_implemented=True,
        )
        for name, _stat in entries:
            yield type(self).from_path(
                provider.path(str(self), name),
                provider,
                provider.path,
                self._providers,
                self._selector,
                pinned=self._pinned,
                logical_segments=(str(self), name),
            )

    def open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        """Open through the selected provider.

        Hand-written rather than generated because the capability gate
        depends on the mode: a read opens under ``open_read``, a write
        under ``open_write``.
        """
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
            # A read has not mutated anything when it reports it cannot open;
            # a write may have created or truncated the file first.
            retry_on_not_implemented=not write,
        )

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
