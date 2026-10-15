"""Plugin dispatch, and the one shape every registry follows."""

import subprocess
import warnings

import pytest

import hostctl
from hostctl import (
    ExecutorProvider,
    HostConfig,
    PosixConfig,
    host_schemes,
    register_system_provider,
    system_provider_names,
)
from hostctl.host import system as system_module


class _EntryPoint:
    def __init__(self, name, factory, loads):
        self.name = name
        self._factory = factory
        self._loads = loads

    def load(self):
        self._loads[self.name] = self._loads.get(self.name, 0) + 1
        return self._factory()


@pytest.fixture
def registry():
    HostConfig._refresh_uri_registry()
    yield
    HostConfig._refresh_uri_registry()


def _make_beta():
    class _BetaConfig(HostConfig, schemes=("regbeta", "regbeta+tls")):
        def __init__(self):
            super().__init__()

        @property
        def connection_uri(self):
            return "regbeta://x"

        def _create_host(self):
            raise NotImplementedError

        @classmethod
        def _from_parsed_uri(cls, parsed, **credentials):
            return cls()

    return _BetaConfig


def _broken():
    raise ImportError("regalpha missing dependency")


def test_a_broken_plugin_does_not_stand_between_other_schemes_and_theirs(registry):
    loads = {}
    beta = []

    def good():
        if not beta:
            beta.append(_make_beta())
        return beta[0]

    HostConfig("local:")
    HostConfig._uri_entry_points = (
        _EntryPoint("regalpha", _broken, loads),
        _EntryPoint("regbeta", good, loads),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        assert type(HostConfig("regbeta+tls://x")).__name__ == "_BetaConfig"
        assert type(HostConfig("regbeta://x")).__name__ == "_BetaConfig"
        with pytest.raises(ValueError, match="unsupported host scheme"):
            HostConfig("regtypo://x")
        for _ in range(2):
            with pytest.raises(RuntimeError, match="failed to load") as caught:
                HostConfig("regalpha://x")
            assert isinstance(caught.value.__cause__, ImportError)

    assert loads == {"regalpha": 1, "regbeta": 1}


def test_the_entry_point_named_for_a_scheme_loads_first(registry):
    loads = {}
    beta = []

    def good():
        if not beta:
            beta.append(_make_beta())
        return beta[0]

    HostConfig("local:")
    HostConfig._uri_entry_points = (
        _EntryPoint("regalpha", _broken, loads),
        _EntryPoint("regbeta", good, loads),
    )
    HostConfig("regbeta://x")

    assert loads == {"regbeta": 1}


def test_a_scheme_another_config_claims_is_refused_at_definition():
    with pytest.raises(ValueError, match="'ssh' is already registered by SshConfig"):

        class _Imposter(HostConfig, schemes=("ssh",)):
            pass

    assert type(HostConfig("ssh://u@nas")).__name__ == "SshConfig"


def test_redefining_a_config_is_not_a_duplicate(registry):
    def define():
        class _Redefined(HostConfig, schemes=("regredefined",)):
            pass

        return _Redefined

    first = define()
    second = define()

    assert second._uri_schemes == ("regredefined",)
    assert first._uri_schemes == ()


def test_replace_takes_a_scheme_over_from_its_owner(registry):
    class _Owner(HostConfig, schemes=("regowned",)):
        pass

    class _Taker(HostConfig, schemes=("regowned",), replace=True):
        pass

    assert _Owner._uri_schemes == ()
    assert _Taker._uri_schemes == ("regowned",)


def test_a_duplicate_system_provider_is_refused_unless_replaced():
    def resolver(config, kind):
        return None

    with pytest.raises(ValueError, match="already registered"):
        register_system_provider("ssh", resolver)

    register_system_provider("regsystem", resolver)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_system_provider("regsystem", resolver)
        register_system_provider("regsystem", resolver, replace=True)
    finally:
        system_module._SYSTEM_PROVIDER_RESOLVERS.pop("regsystem", None)


def test_an_unknown_descriptor_loads_the_system_provider_plugin(monkeypatch):
    def execute(command, *args, **options):
        return subprocess.CompletedProcess((command,), 0, b"", b"")

    def resolver(config, kind):
        return ExecutorProvider("regplug", execute) if kind == "executor" else None

    loads = {}
    monkeypatch.setattr(
        system_module,
        "entry_points_in",
        lambda group: (
            (_EntryPoint("regplug", lambda: resolver, loads),)
            if group == "hostctl.system_providers"
            else ()
        ),
    )
    try:
        host = PosixConfig("n", executor=("regplug",))._create_host()
        assert [p.name for p in host._executor_selector.providers] == ["regplug"]
        assert "regplug" in system_provider_names()
        PosixConfig("n", executor=("regplug",))._create_host()
        assert loads == {"regplug": 1}
    finally:
        system_module._SYSTEM_PROVIDER_RESOLVERS.pop("regplug", None)


def test_the_names_listings_include_the_built_ins():
    assert {"ssh", "winrm", "posix", "local"} <= set(host_schemes())
    assert {"ssh", "sftp", "winrm", "local"} <= set(system_provider_names())
    assert host_schemes() == tuple(sorted(host_schemes()))
    assert system_provider_names() == tuple(sorted(system_provider_names()))
    assert "host_schemes" in hostctl.__all__
    assert "system_provider_names" in hostctl.__all__


def test_the_scheme_listing_names_entry_points_without_importing_them(registry):
    loads = {}
    HostConfig("local:")
    HostConfig._uri_entry_points = (_EntryPoint("regunloaded", _broken, loads),)

    assert "regunloaded" in host_schemes()
    assert loads == {}
