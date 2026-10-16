"""Constructor signatures: what the dispatching metaclasses report, and the
keyword-only wrapper the transport configs use."""

from __future__ import annotations

import abc as _abc
import functools as _functools
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


def _keyword_only(positional: int = 1) -> _ty.Callable[[_ty.Type[_T]], _ty.Type[_T]]:
    """Make every `__init__` parameter after the first `positional` keyword-only.

    Applied above `@dataclasses.dataclass`: the 3.9 floor has neither
    `dataclass(kw_only=)` nor `KW_ONLY`. A type checker still reads the
    generated `__init__`; calls and `inspect.signature` see this one.
    """

    def decorate(cls: _ty.Type[_T]) -> _ty.Type[_T]:
        original = cls.__init__
        signature = _inspect.signature(original)
        parameters = list(signature.parameters.values())
        keep = parameters[: positional + 1]  # `self` and the leading ones
        rest = [
            p.replace(kind=_inspect.Parameter.KEYWORD_ONLY)
            for p in parameters[positional + 1 :]
        ]
        # A descriptor-typed field accepts what its `__set__` takes, which is
        # what a caller passes: show that, not the descriptor's own type.
        annotations = dict(getattr(original, "__annotations__", {}))
        for index, parameter in enumerate(rest):
            accepted = getattr(
                cls.__dict__.get(parameter.name), "init_annotation", None
            )
            if accepted is not None:
                rest[index] = parameter.replace(annotation=accepted)
                annotations[parameter.name] = accepted
        name = cls.__name__
        plural = "" if positional == 1 else "s"

        @_functools.wraps(original)
        def __init__(self: object, *args: object, **kwargs: object) -> None:
            if len(args) > positional:
                raise TypeError(
                    f"{name}() takes {positional} positional argument{plural} "
                    f"but {len(args)} were given"
                )
            original(self, *args, **kwargs)

        setattr(__init__, "__signature__", signature.replace(parameters=keep + rest))
        __init__.__annotations__ = annotations
        setattr(cls, "__init__", __init__)
        return cls

    return decorate
