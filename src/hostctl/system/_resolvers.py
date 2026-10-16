"""System provider descriptors and the transports they resolve to."""

from __future__ import annotations


import typing
from contextvars import ContextVar


from .._registry import entry_points_in
from ._config import SystemConfig

_SYSTEM_PROVIDER_RESOLVERS = {}


def register_system_provider(
    name: str,
    resolver: typing.Callable[["SystemConfig", str], typing.Any],
    *,
    replace: bool = False,
) -> None:
    """Register a provider descriptor resolver for :class:`SystemConfig`.

    `resolver(config, kind)` returns the provider for `kind` (`"executor"` or
    `"path"`), or `None` when the descriptor offers none. A name registered
    already raises `ValueError` unless `replace=True`.
    """
    key = str(name).casefold().strip()
    if not key or not callable(resolver):
        raise ValueError("provider name and callable resolver are required")
    if key in _SYSTEM_PROVIDER_RESOLVERS and not replace:
        raise ValueError(f"system provider is already registered: {key}")
    _SYSTEM_PROVIDER_RESOLVERS[key] = resolver


def system_provider_names() -> typing.Tuple[str, ...]:
    """Every provider descriptor `SystemConfig` accepts, sorted.

    The registered names plus the `hostctl.system_providers` entry points,
    which are listed and not imported.
    """
    names = set(_SYSTEM_PROVIDER_RESOLVERS)
    names.update(
        str(point.name).casefold() for point in entry_points_in(_ENTRY_POINT_GROUP)
    )
    return tuple(sorted(names))


_ENTRY_POINT_GROUP = "hostctl.system_providers"


def _load_system_provider(key: str):
    """Load, register and return the plugin resolver named `key`, if any."""
    for point in entry_points_in(_ENTRY_POINT_GROUP):
        if str(point.name).casefold() != key:
            continue
        try:
            resolver = point.load()
            register_system_provider(key, resolver, replace=True)
        except Exception as exc:
            raise ValueError(f"invalid system provider plugin {key!r}: {exc}") from exc
        return resolver
    return None


def _provider_option(config: "SystemConfig", name: str):
    values = config.options.get("provider_options", {})
    if isinstance(values, dict):
        return values.get(name)
    return None


def _local_provider(config, kind):
    del config
    from ..transports.local._providers import LocalExecutorProvider, LocalPathProvider

    return LocalExecutorProvider() if kind == "executor" else LocalPathProvider()


#: The transports one `_create_host()` call has built, by option key. Scoped to
#: the call (and so to its thread), because the executor and path providers of
#: ONE host must share a connection and two hosts must not.
_TRANSPORTS: "ContextVar[typing.Optional[dict]]" = ContextVar(
    "hostctl_transports", default=None
)


def _shared_transport(key, build):
    """The transport for `key` within this `_create_host()` call, built once."""
    cache = _TRANSPORTS.get()
    if cache is None:
        return build()
    if key not in cache:
        cache[key] = build()
    return cache[key]


def _transport_provider(config, kind, option, label, load):
    """Resolve one provider of a transport-backed pair from `provider_options`.

    `load()` imports lazily: (config type, transport type, executor provider
    type, path provider type).
    """
    config_type, transport_type, executor_type, path_type = load()
    value = _provider_option(config, option)
    if not isinstance(value, config_type):
        raise ValueError(
            f"system {label} descriptors require "
            f"provider_options={{'{option}': {config_type.__name__}(...)}}"
        )
    transport = _shared_transport(option, lambda: transport_type(value))
    return executor_type(transport) if kind == "executor" else path_type(transport)


def _load_ssh():
    from ..transports.ssh._providers import SFTPPathProvider, SSHExecutorProvider
    from ..transports.ssh._config import SSHConfig
    from ..transports.ssh._transport import _SSHTransport

    return SSHConfig, _SSHTransport, SSHExecutorProvider, SFTPPathProvider


def _load_winrm():
    from ..transports.winrm._config import WinRMConfig
    from ..transports.winrm._providers import WinRMExecutorProvider, WinRMPathProvider
    from ..transports.winrm._transport import _WinRMTransport

    return WinRMConfig, _WinRMTransport, WinRMExecutorProvider, WinRMPathProvider


def _ssh_provider(config, kind):
    return _transport_provider(config, kind, "ssh", "SSH", _load_ssh)


def _winrm_provider(config, kind):
    return _transport_provider(config, kind, "winrm", "WinRM", _load_winrm)


register_system_provider("local", _local_provider)

register_system_provider("ssh", _ssh_provider)

register_system_provider("sftp", _ssh_provider)

register_system_provider("winrm", _winrm_provider)
