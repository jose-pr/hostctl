"""Provider descriptors: the names a system host resolves into providers."""

from __future__ import annotations


import inspect
import logging
import typing


from .._extras import _available
from .._registry import entry_points_in
from ..provider._contracts import ExecutorProvider, PathProvider

log = logging.getLogger(__name__)

#: What a resolver returns: the executor and path provider a descriptor
#: offers, either of them `None`.
ProviderPair = typing.Tuple[
    typing.Optional[ExecutorProvider], typing.Optional[PathProvider]
]

#: `resolver(host) -> (executor | None, path | None)`.
ProviderResolver = typing.Callable[["SystemHost"], ProviderPair]

_SYSTEM_PROVIDER_RESOLVERS: typing.Dict[str, ProviderResolver] = {}

#: Descriptors a host's DEFAULTS drop when the extra serving them is missing,
#: by descriptor name, whichever resolver serves the name.
_DESCRIPTOR_EXTRAS = {"ssh": "ssh", "sftp": "ssh"}

_ENTRY_POINT_GROUP = "hostctl.system_providers"


def _key(name: object) -> str:
    return str(name).casefold().strip()


def register_system_provider(
    name: str,
    resolver: ProviderResolver,
    *,
    replace: bool = False,
) -> None:
    """Register a provider descriptor every system host accepts.

    `resolver(host)` returns the `(executor, path)` pair the descriptor
    offers, either of them `None`. A class's own `provider_resolvers` win over
    this registry. A name registered already raises `ValueError` unless
    `replace=True`.
    """
    key = _key(name)
    if not key or not callable(resolver):
        raise ValueError("provider name and callable resolver are required")
    try:
        inspect.signature(resolver).bind(None)
    except TypeError:
        raise TypeError(
            f"system provider resolver {key!r} must take one argument, the host, "
            "and return (executor provider or None, path provider or None)"
        ) from None
    except ValueError:
        # No introspectable signature (a builtin): judged when it is called.
        pass
    if key in _SYSTEM_PROVIDER_RESOLVERS and not replace:
        raise ValueError(f"system provider is already registered: {key}")
    _SYSTEM_PROVIDER_RESOLVERS[key] = resolver


def system_provider_names() -> typing.Tuple[str, ...]:
    """Every globally registered provider descriptor, sorted.

    The registered names plus the `hostctl.system_providers` entry points,
    which are listed and not imported. A host class may add or hide names
    with its own `provider_resolvers`.
    """
    names = set(_SYSTEM_PROVIDER_RESOLVERS)
    names.update(_key(point.name) for point in entry_points_in(_ENTRY_POINT_GROUP))
    return tuple(sorted(names))


def _load_system_provider(key: str) -> typing.Optional[ProviderResolver]:
    """Load, register and return the plugin resolver named `key`, if any."""
    for point in entry_points_in(_ENTRY_POINT_GROUP):
        if _key(point.name) != key:
            continue
        try:
            resolver = point.load()
            register_system_provider(key, resolver, replace=True)
        except Exception as exc:
            raise ValueError(f"invalid system provider plugin {key!r}: {exc}") from exc
        return resolver
    return None


def _class_resolvers(
    host_type: type,
) -> typing.Dict[str, typing.Optional[ProviderResolver]]:
    """`provider_resolvers` merged up the MRO, the most derived class winning."""
    merged: typing.Dict[str, typing.Optional[ProviderResolver]] = {}
    for klass in reversed(host_type.__mro__):
        own = vars(klass).get("provider_resolvers")
        if own:
            merged.update((_key(name), value) for name, value in own.items())
    return merged


def _known_names(own: typing.Mapping[str, object]) -> typing.List[str]:
    names = set(system_provider_names()) | set(own)
    return sorted(name for name in names if own.get(name, True) is not None)


def _default_names(names: typing.Iterable[str], kind: str) -> typing.Tuple[str, ...]:
    """Default descriptors whose extra is installed.

    Only defaults degrade: a name the caller chose is kept, and fails at use
    with the install hint rather than being silently served by another.
    """
    kept = []
    for name in names:
        extra = _DESCRIPTOR_EXTRAS.get(_key(name))
        if extra is not None and not _available(extra):
            log.debug(
                "default %s provider %s dropped: the %r extra is not installed",
                kind,
                name,
                extra,
            )
            continue
        kept.append(name)
    return tuple(kept)


def _resolve(
    host: "SystemHost",
    executor: typing.Iterable[str],
    path: typing.Iterable[str],
) -> typing.Tuple[tuple, tuple]:
    """Resolve descriptor names into `(executor providers, path providers)`.

    One pass: each resolver is called once however many names reach it, so
    the executor and path legs of one transport share it, and two passes (two
    hosts) never do.
    """
    own = _class_resolvers(type(host))
    results: typing.Dict[int, typing.Tuple[ProviderResolver, ProviderPair]] = {}

    def provider(descriptor: str, kind: str, index: int):
        key = _key(descriptor)
        if key in own:
            resolver = own[key]
        else:
            resolver = _SYSTEM_PROVIDER_RESOLVERS.get(key) or _load_system_provider(key)
        if resolver is None:
            raise ValueError(
                f"unknown {kind} provider descriptor: {descriptor!r} "
                f"(known: {', '.join(_known_names(own))})"
            )
        if id(resolver) not in results:
            pair = resolver(host)
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise TypeError(
                    f"provider descriptor {descriptor!r} must resolve to "
                    f"(executor provider or None, path provider or None), "
                    f"not {type(pair).__name__}"
                )
            results[id(resolver)] = (resolver, pair)
        value = results[id(resolver)][1][index]
        if value is None:
            raise ValueError(
                f"provider descriptor {descriptor!r} does not support {kind}"
            )
        return value

    return (
        tuple(provider(name, "executor", 0) for name in executor),
        tuple(provider(name, "path", 1) for name in path),
    )


def _provider_option(config, name: str, config_type: type, label: str):
    values = getattr(config, "options", {}).get("provider_options", {})
    value = values.get(name) if isinstance(values, dict) else None
    if not isinstance(value, config_type):
        raise ValueError(
            f"system {label} descriptors require "
            f"provider_options={{'{name}': {config_type.__name__}(...)}}"
        )
    return value


def _local_pair(host):
    from ..transports.local._host import local_providers

    return local_providers()


def _ssh_pair(host):
    from ..transports.ssh._config import SSHConfig
    from ..transports.ssh._providers import ssh_providers

    return ssh_providers(_provider_option(host.config, "ssh", SSHConfig, "SSH"))


def _winrm_pair(host):
    from ..transports.winrm._config import WinRMConfig
    from ..transports.winrm._providers import winrm_providers

    return winrm_providers(_provider_option(host.config, "winrm", WinRMConfig, "WinRM"))


register_system_provider("local", _local_pair)

register_system_provider("ssh", _ssh_pair)

register_system_provider("sftp", _ssh_pair)

register_system_provider("winrm", _winrm_pair)


# Bound last: `_host` imports this module inside its methods, so either may be
# imported first.
from ._host import SystemHost  # noqa: E402
