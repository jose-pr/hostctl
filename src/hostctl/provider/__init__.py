"""Composable execution and filesystem provider contracts.

The names exported here are the *authoring* surface: the base classes an
integration instantiates or subclasses to reach a new transport, the probe and
selection records it exchanges with a host, and the one exception that permits
fallback.  See the "Systems and providers" guide for the authoring contract.

The built-in transport adapters in :mod:`hostctl.provider.transports` are how
``LocalHost``, ``ContainerHost``, and ``QemuHost`` assemble themselves.  They
are importable from that module but deliberately absent from ``__all__``: no
caller constructs them, and their capability frozensets describe backends
hostctl owns rather than a contract a caller implements against.
"""

from ._common import (
    ExecutorProvider,
    OperationNotStarted,
    PathProvider,
    ProviderProbe,
    ProviderSelection,
    ProviderSelector,
    SessionInitializer,
)

__all__ = [
    "ExecutorProvider",
    "OperationNotStarted",
    "PathProvider",
    "ProviderProbe",
    "ProviderSelection",
    "ProviderSelector",
    "SessionInitializer",
]
