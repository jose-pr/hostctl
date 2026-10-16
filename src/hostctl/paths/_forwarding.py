"""How a composite path forwards each operation to a backend path."""

from __future__ import annotations

import inspect

import itertools

import os


import typing

from pathapi import Path


from ..provider._contracts import PathProvider

from pathapi.protocols import (
    BinaryOpen,
    Chmod,
    Chown,
    Create,
    Hardlink,
    Listing,
    Move,
    NativeChecksum,
    Owned,
    Readlink,
    Remove,
    Resolve,
    Stat,
    Symlink,
    Utime,
)

PathOperation = str


#: The operation each pathapi protocol's primitives are routed under, for
#: `_supports()`. A protocol left out is one whose answer depends on the
#: backend path class as well (`_BACKEND_PROTOCOLS`).
_PROTOCOL_OPERATIONS: "dict[type, tuple[PathOperation, ...]]" = {
    Stat: ("stat",),
    Listing: ("scandir",),
    BinaryOpen: ("open_read",),
    Create: ("mkdir",),
    Remove: ("unlink", "rmdir"),
    Chmod: ("chmod",),
    Readlink: ("readlink",),
    Symlink: ("symlink_to",),
    Move: ("rename",),
}


#: Protocols a backend path class may or may not implement whatever the
#: provider declares: the declared operation says the provider routes the call,
#: the class says it can do it.
_BACKEND_PROTOCOLS: "dict[type, PathOperation]" = {
    Utime: "utime",
    Hardlink: "hardlink_to",
    Owned: "stat",
    Resolve: "stat",
    NativeChecksum: "read",
    Chown: "chmod",
}


def _provider_supports(
    provider: PathProvider,
    operation: PathOperation,
    capabilities: typing.Optional[typing.AbstractSet[str]] = None,
) -> bool:
    """Whether `capabilities` (default: the declared set) cover `operation`."""
    if capabilities is None:
        capabilities = provider.capabilities
    # `a|b` means "either will do": a stat-derived predicate is satisfied by
    # `stat` as well as by its own name, and `rm` by `unlink` or `rmdir`.
    alternatives = operation.split("|")
    return bool(
        "path" in capabilities
        or any(value in capabilities for value in alternatives)
        # A bare `open` stands in for `open_read` only: a mutation has to be
        # declared, so a read-only provider (a QGA guest with no write RPCs)
        # is never chosen for `open('wb')` over a writable sibling.
        or ("open_read" in alternatives and "open" in capabilities)
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


def _logical_text(backend_path: Path) -> str:
    """The path text a backend path names *on its own host*.

    `str()` of a URI-shaped backend path is the whole URI, which is not a
    path on the target: used as a logical path it would send I/O to
    `/sftp:/host:22/srv/x`. `__fspath__()` is what a host-filesystem scheme
    answers with; a backend with no filesystem spelling (`MemPath`,
    `SFTPPath`) is not `os.PathLike`, and for those `str()` is already the
    path text.
    """
    try:
        return os.fspath(backend_path)
    except (NotImplementedError, TypeError, ValueError):
        return str(backend_path)


def _resolves_on(target: object, provider: typing.Optional[PathProvider]) -> bool:
    """Whether `target` names a path the given provider can address itself.

    Only then may a transfer take the backend's own ``copy``/``move``. Anything
    else -- a foreign ``Path``, a composite pinned elsewhere, one that does not
    carry this provider at all -- is a real cross-backend transfer.
    """
    from ._mixin import _CompositePathMixin

    if provider is None or not isinstance(target, _CompositePathMixin):
        return False
    if target._pinned and target.provider is not provider:
        return False
    return any(item is provider for item in target.providers)


def _is_composite_owner(base: type) -> bool:
    """True for classes belonging to this module's composite hierarchy.

    Tells a deliberate composite override (``iterdir``, ``rename``,
    ``readlink``, ``copy``) apart from an inherited ``pathapi``
    implementation, which is exactly what forwarding must displace.
    """
    return getattr(base, "__module__", "") == __name__


def _argument_for(argument: object, dispatched: Path, provider) -> object:
    """One call argument, expressed in terms the backend can accept."""
    from ._mixin import _CompositePathMixin

    if not isinstance(argument, _CompositePathMixin):
        return argument
    if _resolves_on(argument, provider):
        candidate = argument._provider_path(provider)
        if type(candidate) is type(dispatched):
            return candidate
    return str(argument)


def _primed(entries: typing.Iterable) -> typing.Iterator:
    """Pull the first entry now, and re-chain it.

    Every backend `_scandir` is a generator, so `iter(path._scandir())` ran
    NOTHING inside the dispatch: a listing that refuses raised during
    iteration, outside the try, and the row's `retry_on_not_implemented` and
    the `OperationNotStartedError` fallback never applied to listing at all.
    """
    iterator = iter(entries)
    try:
        first = next(iterator)
    except StopIteration:
        return iter(())
    return itertools.chain((first,), iterator)


def _rebuild_composite(cls, text, provider, factory, providers, selector, pinned):
    """Reconstruct a composite path from its routing state (see __reduce__)."""
    return cls(
        text,
        backend_path=None,
        provider=provider,
        factory=factory,
        providers=providers,
        selector=selector,
        pinned=pinned,
    )


def _make_forwarder(
    name: str,
    capability: PathOperation,
    pin: bool,
    retry_safe: bool,
    rewrap: bool = False,
) -> typing.Callable[..., object]:
    """Build a method forwarding ``name`` to the selected backend path."""

    def forwarder(self, *args, **kwargs):
        def call(path: Path):
            method = getattr(path, name, None)
            if method is None:
                raise NotImplementedError(
                    f"{type(path).__name__} does not support {name}"
                )
            # A composite argument is meaningless to the backend, which would
            # re-parse it through its own constructor -- and for a backend
            # whose paths carry state that constructor FAILS:
            # `samefile(composite)` raised `TypeError: WinRMPath requires a
            # backend`, which is not even the `NotImplementedError` the row's
            # retry contract is written against. When the argument lives on
            # the same provider, hand over that provider's own path; anything
            # else degrades to the logical string, which is right for a
            # symlink target and is all a foreign path can offer.
            resolved = tuple(_argument_for(arg, path, self._provider) for arg in args)
            return method(*resolved, **_accepts_kwargs(method, kwargs, name))

        if rewrap:
            result, provider = self._dispatch(
                capability,
                call,
                pin=pin,
                with_provider=True,
                retry_on_not_implemented=retry_safe,
            )
            return self._as_composite(result, provider)
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


class _Row(typing.NamedTuple):
    capability: str
    pin: bool
    retry_safe: bool
    #: The result is a backend path: hand it back as a composite path.
    rewrap: bool = False


# Methods forwarded verbatim to the selected backend path.  Each entry maps a
# method name to the capability string gating it, whether the call pins the
# provider, and whether a `NotImplementedError` from it may fall through to the
# next provider.
#
# The method that was *called* is the method invoked on the backend -- never a
# decomposition into primitives.  Backends override derived operations for real
# optimization (``SFTPPath.copy`` fans out over asyncssh workers,
# ``SFTPPath.rm``/``checksum`` run server-side, ``LocalPath`` reaches ``shutil``
# and ``os.scandir``), and decomposing would silently discard all of it while
# still producing correct results.  Anything the backend does not override
# resolves to ``pathapi``'s own wrapper, so operations added upstream work
# by adding a row here rather than writing a body.
#
# ``retry_safe`` is deliberately conservative.  A wrapper composed of several
# primitives may already have mutated when a later primitive raises --
# ``Path.symlink_to(force=True)`` unlinks *before* calling ``_symlink_to``, so a
# backend lacking that primitive deletes the entry and only then raises.
# ``NotImplementedError`` cannot distinguish "did nothing" from "did half", so
# only calls that cannot mutate before raising opt in.
_FORWARDED: "dict[str, _Row]" = {
    # The stat-derived predicates accept EITHER their own capability or
    # `stat`, because that is what they are: `is_file()` is a stat with a
    # mask. Gating them on their own name alone meant a read-only provider
    # answered `stat()` and `is_symlink()` and refused
    # `is_file()` -- and every pathapi helper that asks `is_dir()` first
    # (copy(recursive), mkdir(exist_ok), move) broke on such a path.
    "exists": ("exists|stat", False, True),
    "is_file": ("is_file|stat", False, True),
    "is_dir": ("is_dir|stat", False, True),
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
    "chown": ("chmod", True, True),
    "chmod": ("chmod", True, True),
    "lchmod": ("chmod", True, True),
    "write_bytes": ("write", True, False),
    "write_text": ("write", True, False),
    "mkdir": ("mkdir", True, False),
    "touch": ("write", True, False),
    "unlink": ("unlink", True, False),
    "rmdir": ("rmdir", True, False),
    # `rm` removes files AND directories, and recursively lists to do it.
    "rm": ("unlink|rmdir", True, False),
    "symlink_to": ("symlink_to", True, False),
    # `_utime` is what `PathSyncer` calls to carry a source's mtime across, so
    # the primitive is forwarded as well as the public names.
    "_utime": ("utime", True, True),
    "hardlink_to": ("hardlink_to", True, True),
    "owner": ("stat", False, True),
    "group": ("stat", False, True),
    "resolve": ("stat", False, True, True),
}
