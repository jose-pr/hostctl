"""Compatibility-facade adapters for the provider-based host layer."""

from __future__ import annotations

import typing

from ..provider import ExecutorProvider, PathProvider


class TransportHostFacade:
    """Expose a legacy transport host through the common provider API."""

    _facade_executor_capabilities: typing.Iterable[object] = ()

    def as_system_host(self):
        """Return a provider-based view without changing legacy ownership."""
        from .system import SystemHost

        owner = self
        executor = ExecutorProvider(
            "legacy",
            lambda command, *args, **options: owner.executor(command, *args, **options),
            capabilities=owner._facade_executor_capabilities,
        )
        path = PathProvider("legacy", lambda *segments: owner.path(*segments))
        configured_dialect = getattr(owner.config, "dialect", None)
        shell = None if configured_dialect == "auto" else owner.shell_flavour
        return SystemHost(
            owner.config,
            executor_providers=(executor,),
            path_providers=(path,),
            shell=shell,
        )
