"""Public annotations must resolve on the oldest supported Python.

`typing.get_type_hints` evaluates every annotation. Under
`from __future__ import annotations` a PEP 604 `X | None`, or a name imported
only for type checking, parses fine and then raises when a type-driven tool
(or the docs' API reference) reads the signature -- on Python 3.9, and for the
names, on every version.
"""

from __future__ import annotations

import inspect
import typing

import pytest

import hostctl


def _public_callables():
    """Each exported function, and each exported class's own public methods,
    property getters and `__init__`."""
    found = []
    for name in hostctl.__all__:
        value = getattr(hostctl, name)
        if inspect.isfunction(value):
            found.append((name, value))
        elif inspect.isclass(value):
            for attr, raw in vars(value).items():
                if attr.startswith("_") and attr != "__init__":
                    continue
                function = raw.fget if isinstance(raw, property) else raw
                function = getattr(function, "__func__", function)
                if inspect.isfunction(function):
                    found.append((f"{name}.{attr}", function))
    return found


_CALLABLES = _public_callables()


@pytest.mark.parametrize(
    "qualname,function", _CALLABLES, ids=[name for name, _ in _CALLABLES]
)
def test_annotation_resolves(qualname, function):
    typing.get_type_hints(function)


def test_the_walk_finds_the_public_surface():
    # A walk that finds nothing would pass every test above.
    names = {name for name, _ in _CALLABLES}

    assert len(names) > 100
    assert {"Host.run", "SystemHost.run", "host_checksum"} <= names


def test_system_host_run_is_annotated():
    signature = inspect.signature(hostctl.SystemHost.run)

    assert signature.return_annotation is not inspect.Signature.empty
    assert [
        name
        for name, parameter in signature.parameters.items()
        if name != "self" and parameter.annotation is inspect.Parameter.empty
    ] == []
    assert list(signature.parameters) == list(
        inspect.signature(hostctl.Host.run).parameters
    )
