"""What the dispatching metaclasses report as a constructor's signature."""

from __future__ import annotations

import abc as _abc


import inspect as _inspect


import typing as _ty

_T = _ty.TypeVar("_T")


if _ty.TYPE_CHECKING:
    # A checker that sees `HostConfig`/`Host` as abstract rejects the
    # dispatching `HostConfig(uri)` call; the abstract-method check is the
    # runtime's, which refuses to build an incomplete subclass.
    def _abstract(function: _T) -> _T:
        return function

else:
    _abstract = _abc.abstractmethod


#: What `HostConfig(...)` and `Host(...)` themselves accept.
_DISPATCH_SIGNATURE = _inspect.Signature(
    [
        _inspect.Parameter(
            "connection_string",
            _inspect.Parameter.POSITIONAL_ONLY,
            annotation="str",
        ),
        _inspect.Parameter("secrets", _inspect.Parameter.VAR_KEYWORD, annotation="str"),
    ]
)


def _constructor_signature(cls: type, base: type) -> _inspect.Signature:
    """The signature `inspect.signature(cls)` reports through a dispatching
    metaclass: the dispatch form for the base, `__init__` minus `self` for a
    subclass, and a `__signature__` the class sets itself, as it is."""
    own = cls.__dict__.get("__signature__")
    if isinstance(own, _inspect.Signature):
        return own
    if cls is base:
        return _DISPATCH_SIGNATURE
    init = _inspect.signature(cls.__init__)
    parameters = list(init.parameters.values())[1:]
    return init.replace(
        parameters=parameters, return_annotation=_inspect.Signature.empty
    )
