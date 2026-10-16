"""Entry-point discovery shared by the scheme and system-provider registries."""

from __future__ import annotations


import importlib.metadata as _metadata
import typing as _ty

if _ty.TYPE_CHECKING:
    from .host._config import HostConfig


def entry_points_in(group: str) -> tuple:
    """The installed entry points of `group`, without importing any."""
    points = _metadata.entry_points()
    if hasattr(points, "select"):
        points = points.select(group=group)
    elif hasattr(points, "get"):
        points = points.get(group, ())
    else:
        points = tuple(item for item in points if getattr(item, "group", None) == group)
    return tuple(points)


def host_schemes() -> tuple[str, ...]:
    """Every scheme `HostConfig(uri)` can dispatch, sorted.

    The schemes of the registered configs, plus the names of the
    `hostctl.configs` entry points, which are listed and not imported.
    """
    from .host._config import HostConfig

    names = {
        scheme
        for implementation in HostConfig._uri_implementations()
        for scheme in implementation._uri_schemes
    }
    for point in HostConfig._uri_entry_points or ():
        names.add(str(getattr(point, "name", "")).casefold())
    names.discard("")
    return tuple(sorted(names))


def _recursive_subclasses(
    base: _ty.Type[HostConfig],
) -> _ty.Iterator[_ty.Type[HostConfig]]:
    for subclass in base.__subclasses__():
        yield subclass
        yield from _recursive_subclasses(subclass)
