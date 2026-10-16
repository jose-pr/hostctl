"""Composable execution and filesystem provider contracts.

The names exported here are the *authoring* surface: the base classes an
integration instantiates or subclasses to reach a new transport, and the probe
and selection records it exchanges with a host. The one exception that permits
fallback, `OperationNotStartedError`, lives in :mod:`hostctl.exceptions`. See
the "Systems and providers" guide for the authoring contract.
"""

from ._contracts import (
    READ_ONLY_PATH_OPERATIONS as READ_ONLY_PATH_OPERATIONS,
    ExecutorProvider as ExecutorProvider,
    PathProvider as PathProvider,
    ProviderDetail as ProviderDetail,
    ProviderProbe as ProviderProbe,
    ProviderSelection as ProviderSelection,
    SelectionStep as SelectionStep,
    SessionInitializer as SessionInitializer,
)
from ._selector import ProviderSelector as ProviderSelector

__all__ = [
    "ExecutorProvider",
    "PathProvider",
    "ProviderDetail",
    "ProviderProbe",
    "ProviderSelection",
    "ProviderSelector",
    "READ_ONLY_PATH_OPERATIONS",
    "SelectionStep",
    "SessionInitializer",
]
