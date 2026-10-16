"""Provider descriptors resolve against the host, per host class.

A resolver receives the host being built and returns the `(executor, path)`
pair its descriptor offers. A host class declares its own descriptors in
`provider_resolvers`, merged up the MRO, ahead of the global registry; its
`default_providers()` applies when the config names none, minus an `ssh`/
`sftp` default whose extra is missing.
"""

from __future__ import annotations

import subprocess

import pytest
from pathapi import Path

from hostctl import (
    ExecutorProvider,
    HostConfig,
    PathProvider,
    PosixConfig,
    PosixHost,
    SSHConfig,
    register_system_provider,
)
from hostctl.system import _resolvers as resolvers_module


def _execute(command, *args, **options):
    return subprocess.CompletedProcess((command, *args), 0, b"", b"")


class _Calls:
    """A resolver serving one executor and one path provider, counting calls."""

    def __init__(self, name):
        self.name = name
        self.hosts = []

    def __call__(self, host):
        self.hosts.append(host)
        return (
            ExecutorProvider(self.name, _execute, capabilities=("args",)),
            PathProvider(f"{self.name}-path", lambda *segments: Path(*segments)),
        )


def _names(providers):
    return [provider.name for provider in providers]


def test_a_class_resolver_receives_the_host_being_built():
    calls = _Calls("app")

    class AppHost(PosixHost):
        provider_resolvers = {"app": calls}

    host = AppHost(PosixConfig("node", executor=("app",), path=("app",)))

    assert calls.hosts == [host]
    assert _names(host.executor_providers) == ["app"]
    assert _names(host.path_providers) == ["app-path"]


def test_one_resolver_reached_by_two_names_runs_once_per_host():
    """The executor and path legs of one transport share it; two hosts never do."""
    calls = _Calls("shared")

    class AppHost(PosixHost):
        provider_resolvers = {"run": calls, "files": calls}

    config = PosixConfig("node", executor=("run",), path=("files",))
    first, second = AppHost(config), AppHost(config)

    assert calls.hosts == [first, second]
    assert first.executor_providers[0] is not second.executor_providers[0]


def test_the_built_in_ssh_descriptors_share_one_transport():
    host = PosixConfig(
        "node",
        executor=("ssh",),
        path=("sftp",),
        provider_options={"ssh": SSHConfig("example.invalid")},
    ).open()

    assert host.executor_providers[0].transport is host.path_providers[0].transport


def test_a_class_descriptor_does_not_leak_into_other_hosts():
    class AppHost(PosixHost):
        provider_resolvers = {"apponly": _Calls("apponly")}

    with pytest.raises(ValueError, match="unknown executor provider descriptor"):
        PosixHost(PosixConfig("node", executor=("apponly",)))
    assert _names(
        AppHost(PosixConfig("node", executor=("apponly",))).executor_providers
    )


def test_resolvers_merge_up_the_mro_and_none_hides_a_name():
    class Base(PosixHost):
        provider_resolvers = {"one": _Calls("base-one"), "two": _Calls("two")}

    class Derived(Base):
        provider_resolvers = {"one": _Calls("derived-one"), "winrm": None}

    host = Derived(PosixConfig("node", executor=("one", "two")))
    assert _names(host.executor_providers) == ["derived-one", "two"]

    with pytest.raises(ValueError, match="'winrm'") as caught:
        Derived(PosixConfig("node", executor=("winrm",)))
    assert "winrm" not in str(caught.value).split("known:")[1]


def test_an_unknown_name_lists_the_names_this_host_knows():
    class AppHost(PosixHost):
        provider_resolvers = {"webshell": _Calls("webshell")}

    with pytest.raises(ValueError) as caught:
        AppHost(PosixConfig("node", executor=("shh",)))

    message = str(caught.value)
    assert "'shh'" in message
    for name in ("local", "ssh", "sftp", "webshell", "winrm"):
        assert name in message


def test_a_descriptor_without_the_kind_names_it():
    def path_only(host):
        return None, PathProvider("rpc", lambda *segments: Path(*segments))

    class AppHost(PosixHost):
        provider_resolvers = {"rpc": path_only}

    assert _names(AppHost(PosixConfig("n", path=("rpc",))).path_providers) == ["rpc"]
    with pytest.raises(ValueError, match="'rpc' does not support executor"):
        AppHost(PosixConfig("n", executor=("rpc",)))


def test_a_resolver_must_return_a_pair():
    class AppHost(PosixHost):
        provider_resolvers = {"bad": lambda host: ExecutorProvider("bad", _execute)}

    with pytest.raises(TypeError, match="'bad' must resolve to"):
        AppHost(PosixConfig("n", executor=("bad",)))


class _Defaults(PosixHost):
    provider_resolvers = {"app": _Calls("app")}

    def default_providers(self):
        return ("ssh", "app"), ("sftp", "app")


def _ssh_config(**names):
    return PosixConfig(
        "node", provider_options={"ssh": SSHConfig("example.invalid")}, **names
    )


def test_defaults_apply_per_kind_when_the_config_names_none():
    host = _Defaults(_ssh_config(path=("app",)))

    assert _names(host.executor_providers) == ["ssh", "app"]
    assert _names(host.path_providers) == ["app-path"]


def test_an_explicitly_empty_kind_composes_no_provider():
    host = _Defaults(_ssh_config(executor=()))

    assert host.executor_providers == ()
    assert "run" not in host.capabilities


def test_defaults_drop_ssh_and_sftp_when_the_extra_is_missing(monkeypatch):
    monkeypatch.setattr(resolvers_module, "_available", lambda extra: False)

    host = _Defaults(_ssh_config())

    assert _names(host.executor_providers) == ["app"]
    assert _names(host.path_providers) == ["app-path"]


def test_a_named_ssh_provider_is_kept_when_the_extra_is_missing(monkeypatch):
    """Serving another transport than the one asked for would be its own bug."""
    monkeypatch.setattr(resolvers_module, "_available", lambda extra: False)

    host = _Defaults(_ssh_config(executor=("ssh",), path=("sftp",)))

    assert _names(host.executor_providers) == ["ssh"]
    assert _names(host.path_providers) == ["sftp"]


def test_explicit_providers_skip_resolution():
    calls = _Calls("app")

    class AppHost(PosixHost):
        provider_resolvers = {"app": calls}

    given = ExecutorProvider("given", _execute)
    host = AppHost(
        PosixConfig("node", executor=("app",), path=("app",)),
        executor_providers=(given,),
    )

    assert host.executor_providers == (given,)
    assert _names(host.path_providers) == ["app-path"]


def test_resolve_providers_rebuilds_for_replace_providers():
    calls = _Calls("app")

    class AppHost(PosixHost):
        provider_resolvers = {"app": calls}

        def default_providers(self):
            return ("app",), ("app",)

    host = AppHost()
    before = host.executor_providers[0]

    executors, paths = host.resolve_providers()
    host.replace_providers(executor=executors, path=paths)

    assert host.executor_providers[0] is not before
    assert _names(host.resolve_providers(executor=(), path=("app",))[1]) == ["app-path"]
    with pytest.raises(TypeError, match="not a string"):
        host.resolve_providers(executor="app")


def test_a_config_without_names_round_trips_as_not_named():
    config = HostConfig("posix://node")

    assert config.executors is None and config.paths is None
    assert HostConfig(str(PosixConfig("n", executor=("local",)))).executors == (
        "local",
    )


def test_a_system_host_built_directly_resolves_its_config():
    host = PosixHost(PosixConfig("node", executor=("local",), path=("local",)))

    assert _names(host.executor_providers) == ["local"]
    assert _names(host.path_providers) == ["local"]


def test_an_old_style_resolver_is_refused_at_registration():
    def old(config, kind):
        return None

    with pytest.raises(TypeError, match="one argument, the host"):
        register_system_provider("regold", old)
    assert "regold" not in resolvers_module._SYSTEM_PROVIDER_RESOLVERS
