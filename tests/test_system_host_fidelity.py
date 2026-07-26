import subprocess

import pytest

from hostctl import (
    ExecutorProvider,
    HostConfig,
    HostInfo,
    OperationNotStarted,
    PosixHost,
    ProviderProbe,
    ProviderSelector,
    SshConfig,
)


def test_selection_trace_has_generation_policy_pin_and_redaction():
    provider = ExecutorProvider(
        "ssh?password=secret",
        lambda command, **options: subprocess.CompletedProcess(command, 0, b"", b""),
    )
    selector = ProviderSelector((provider,))

    selected = selector.select(policy="fallback", pin=True)
    item = selected.trace[0]
    assert item["generation"] == 0
    assert item["policy"] == "fallback"
    assert item["pin"] is True
    assert selected.generation == 0
    assert selected.policy == "fallback"
    assert selected.pinned is True
    assert "secret" not in item["provider"]

    selector.invalidate()
    assert selector.select().generation == 1


def test_provider_details_probe_without_dispatch_and_capabilities_filter():
    calls = []

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b"", b"")

    unavailable = ExecutorProvider(
        "offline", execute, probe=lambda: ProviderProbe("unavailable", "offline")
    )
    available = ExecutorProvider("online", execute, capabilities=("args",))
    host = PosixHost(executor_providers=(unavailable, available))

    details = host.provider_details
    assert [item["name"] for item in details] == ["offline", "online"]
    assert details[0]["availability"] == "unavailable"
    assert host.capabilities == frozenset(("run",))
    assert host.executor_capabilities == frozenset(("args",))
    assert calls == []


def test_system_config_roundtrip_accepts_constructor_only_provider_options():
    ssh = SshConfig("node", username="root")
    config = HostConfig(
        "posix://node?executor=ssh&path=sftp",
        provider_options={"ssh": ssh},
    )
    assert str(config) == "posix://node?executor=ssh&path=sftp"
    restored = HostConfig(
        str(config), provider_options={"ssh": ssh}, initializer=lambda session: None
    )
    assert restored.executors == ("ssh",)
    assert restored.paths == ("sftp",)
    assert restored._create_host().capabilities == frozenset(("run", "path"))

    with pytest.raises(ValueError, match="unsupported credentials"):
        HostConfig(str(config), password="secret")


def test_info_merges_first_non_none_fields_and_preserves_system_family():
    first = ExecutorProvider("first", lambda *args, **kwargs: None)
    first.info = lambda: HostInfo(hostname="remote", os_name="Linux")
    second = ExecutorProvider("second", lambda *args, **kwargs: None)
    second.info = lambda: HostInfo(os_version="6.8", architecture="x86_64")
    host = PosixHost(executor_providers=(first, second))

    assert host.info() == HostInfo(
        hostname="remote",
        os_family="posix",
        os_name="Linux",
        os_version="6.8",
        architecture="x86_64",
    )


def test_started_failure_never_replays_on_next_provider():
    calls = []

    def started(*args, **kwargs):
        calls.append("started")
        raise RuntimeError("remote operation started")

    def fallback(*args, **kwargs):
        calls.append("fallback")
        return subprocess.CompletedProcess(args, 0, b"ok", b"")

    host = PosixHost(
        executor_providers=(
            ExecutorProvider("first", started),
            ExecutorProvider("second", fallback),
        )
    )
    with pytest.raises(RuntimeError, match="started"):
        host.run("echo hi", check=False)
    assert calls == ["started"]
