"""Config and host constructors report their real parameters.

`Host(uri)` and `HostConfig(uri)` dispatch through a metaclass `__call__`;
without a `__signature__` every subclass showed that `(*args, **options)`
form to `inspect.signature()` and `help()`.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

import hostctl
from hostctl.host._host import Host
from hostctl.host._config import HostConfig

_CLASSES = [
    getattr(hostctl, name)
    for name in hostctl.__all__
    if isinstance(getattr(hostctl, name), type)
    and issubclass(getattr(hostctl, name), (Host, HostConfig))
    and getattr(hostctl, name) not in (Host, HostConfig)
]


def test_every_config_and_host_is_covered():
    names = {cls.__name__ for cls in _CLASSES}
    assert {"LocalConfig", "LocalHost", "WinRMConfig", "SystemHost"} <= names


@pytest.mark.parametrize("cls", _CLASSES, ids=lambda cls: cls.__name__)
def test_the_signature_is_the_constructor_not_the_metaclass(cls):
    signature = inspect.signature(cls)
    parameters = list(signature.parameters.values())

    if parameters:
        assert parameters[0].name not in ("args", "self"), signature
    assert signature.return_annotation is inspect.Signature.empty
    if dataclasses.is_dataclass(cls):
        fields = {field.name for field in dataclasses.fields(cls) if field.init}
        assert fields <= set(signature.parameters), signature


@pytest.mark.parametrize("cls", [HostConfig, Host], ids=lambda cls: cls.__name__)
def test_the_dispatching_bases_show_the_connection_string(cls):
    signature = inspect.signature(cls)

    assert list(signature.parameters) == ["connection_string", "secrets"]
    assert (
        signature.parameters["connection_string"].kind
        is inspect.Parameter.POSITIONAL_ONLY
    )


def test_a_class_that_sets_its_own_signature_keeps_it():
    own = inspect.Signature([inspect.Parameter("x", inspect.Parameter.KEYWORD_ONLY)])

    class Custom(hostctl.LocalConfig):
        __signature__ = own

    assert inspect.signature(Custom) is own


def test_calling_a_subclass_still_constructs_it():
    config = hostctl.LocalConfig()

    assert type(config) is hostctl.LocalConfig
    assert isinstance(HostConfig("local:"), hostctl.LocalConfig)
