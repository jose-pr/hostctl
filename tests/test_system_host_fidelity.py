import subprocess
import threading
import time

import pytest

from hostctl.provider._contracts import ExecutorProvider, PathProvider, ProviderProbe
from hostctl.provider._selector import ProviderSelector
from hostctl.host._config import HostConfig
from hostctl.host._info import HostInfo
from hostctl.system._kinds import IOSHost, PosixHost, WindowsHost
from hostctl.exceptions import OperationNotStartedError
from hostctl.transports.ssh._config import SSHConfig


def test_selection_trace_has_generation_policy_pin_and_redaction():
    provider = ExecutorProvider(
        "ssh?password=secret",
        lambda command, **options: subprocess.CompletedProcess(command, 0, b"", b""),
    )
    selector = ProviderSelector((provider,))

    selected = selector.select(pin=True)
    item = selected.trace[0]
    assert item["generation"] == 0
    assert item["policy"] == "ordered"
    assert item["pin"] is True
    assert selected.generation == 0
    assert selected.pinned is True
    assert "secret" not in item["provider"]

    selector.invalidate()
    assert selector.select().generation == 1


def test_provider_selector_rejects_ambiguous_duplicate_names():
    first = ExecutorProvider("duplicate", lambda *args, **kwargs: None)
    second = ExecutorProvider("duplicate", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="unique"):
        ProviderSelector((first, second))


def test_provider_details_probe_without_dispatch_and_capabilities_filter():
    calls = []
    probes = []

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b"", b"")

    unavailable = ExecutorProvider(
        "offline",
        execute,
        probe=lambda: probes.append("offline")
        or ProviderProbe("unavailable", "offline"),
    )
    available = ExecutorProvider(
        "online",
        execute,
        capabilities=("args",),
        probe=lambda: probes.append("online") or ProviderProbe("available"),
    )
    host = PosixHost(executor_providers=(unavailable, available))

    details = host.provider_details
    assert [item["name"] for item in details] == ["offline", "online"]
    assert details[0]["availability"] == "unavailable"
    assert host.capabilities == frozenset(("run",))
    assert host.executor_capabilities == frozenset(("args",))
    assert calls == []
    assert probes == ["offline", "online"]


def test_system_config_roundtrip_accepts_constructor_only_provider_options():
    ssh = SSHConfig("node", username="root")
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
    # SSH providers open sessions, so the host reports spawn/tty as well.
    assert restored._create_host().capabilities == frozenset(
        ("run", "path", "spawn", "tty")
    )

    with pytest.raises(ValueError, match="unsupported credentials"):
        HostConfig(str(config), password="secret")


def test_info_merges_first_non_none_fields_and_preserves_system_family():
    first = ExecutorProvider("first", lambda *args, **kwargs: None)
    first.info = lambda: HostInfo(hostname="remote", os_family="linux", os_name="Linux")
    second = ExecutorProvider("second", lambda *args, **kwargs: None)
    second.info = lambda: HostInfo(os_version="6.8", architecture="x86_64")
    host = PosixHost(executor_providers=(first, second))

    assert host.info() == HostInfo(
        hostname="remote",
        os_family="linux",
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


def test_composite_mutation_selection_trace_marks_pin(tmp_path):
    from pathapi import Path as HostPath

    provider = PathProvider("local", lambda *parts: HostPath(*parts))
    path = PosixHost(path_providers=(provider,)).path(tmp_path, "value")
    path.write_bytes(b"payload")

    assert path.selection_trace[-1]["pin"] is True


def test_concurrent_run_connects_each_provider_exactly_once():
    """Racing callers must not repeat connects or duplicate the connected list.

    Without a lock around `_ensure_provider_connected`'s check-then-append,
    N concurrent `run()` calls all observe "not connected", all dial out, and
    all append the same provider to `_connected_providers`.
    """

    class _CountingTransport:
        def __init__(self):
            self.connect_calls = 0
            self._lock = threading.Lock()

        def connect(self):
            with self._lock:
                self.connect_calls += 1
            # A real dial-out is slow enough to widen the race window; without
            # it every thread would likely serialize on the GIL by accident.
            time.sleep(0.05)

        def close(self):
            pass

    class _CountingProvider(ExecutorProvider):
        def __init__(self, transport):
            self.transport = transport
            super().__init__(
                "counting",
                lambda command, *args, **options: subprocess.CompletedProcess(
                    command, 0, b"", b""
                ),
                capabilities=("args",),
            )

        def connect(self):
            self.transport.connect()

    transport = _CountingTransport()
    host = PosixHost(executor_providers=(_CountingProvider(transport),))

    workers = 12
    barrier = threading.Barrier(workers)
    errors = []

    def worker():
        try:
            barrier.wait()
            host.run("echo hi", check=False)
        except BaseException as exc:  # pragma: no cover - surfaced by assert
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert transport.connect_calls == 1
    assert len(host._connected_providers) == 1


def test_concurrent_connect_and_close_keep_lifecycle_state_consistent():
    """`connect()`/`close()` share the lock, so state never interleaves."""

    class _Transport:
        def __init__(self):
            self.connects = 0
            self._lock = threading.Lock()

        def connect(self):
            with self._lock:
                self.connects += 1
            time.sleep(0.01)

        def close(self):
            pass

    class _Provider(ExecutorProvider):
        def __init__(self, transport):
            self.transport = transport
            super().__init__(
                "lifecycle",
                lambda command, *args, **options: subprocess.CompletedProcess(
                    command, 0, b"", b""
                ),
                capabilities=("args",),
            )

        def connect(self):
            self.transport.connect()

        def close(self):
            self.transport.close()

    transport = _Transport()
    host = PosixHost(executor_providers=(_Provider(transport),))

    workers = 8
    barrier = threading.Barrier(workers)
    errors = []

    def worker():
        try:
            barrier.wait()
            host.connect()
        except BaseException as exc:  # pragma: no cover - surfaced by assert
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert transport.connects == 1
    assert host._connected is True
    assert len(host._connected_providers) == 1

    host.close()
    assert host._connected is False
    assert host._connected_providers == []


def test_session_initializer_may_reenter_run_without_deadlocking():
    """The lifecycle lock is reentrant: connect() -> initializer -> run()."""

    def execute(command, *args, **options):
        return subprocess.CompletedProcess(command, 0, b"", b"")

    seen = []

    def initializer(host):
        # A session initializer receives the connecting host and is entitled
        # to use it; a non-reentrant lock would deadlock right here.
        seen.append(host.run("echo bootstrap", check=False))

    host = PosixHost(
        executor_providers=(ExecutorProvider("init", execute, capabilities=("args",)),),
        initializer=initializer,
    )
    host.connect()

    assert len(seen) == 1
    assert host._connected is True


def test_abstract_system_config_fails_clearly_instead_of_attribute_error():
    """`SystemConfig` is abstract; it must say so, not leak an AttributeError.

    It binds neither `host_type` nor `uri_scheme`, so it must neither raise
    a bare `AttributeError` from `_create_host()` nor advertise a `system://`
    URI that `HostConfig(...)` would reject.
    """
    from hostctl.system._config import SystemConfig

    config = SystemConfig("node")

    with pytest.raises(TypeError, match="abstract and creates no host"):
        config._create_host()

    with pytest.raises(NotImplementedError, match="no connection URI"):
        config.connection_uri

    # The error names the concrete alternatives.
    with pytest.raises(TypeError, match="PosixConfig, WindowsConfig, IOSConfig"):
        config._create_host()


def test_system_config_scheme_stays_the_derived_hostconfig_property():
    """`scheme` is one thing across the hierarchy: a URI-derived property.

    A concrete config must not shadow `HostConfig.scheme` with a plain
    string, which `SystemHost.__init__` could then assign to.
    """
    from hostctl.host._config import HostConfig as _HostConfig
    from hostctl.system._kinds import IOSConfig, PosixConfig, WindowsConfig
    from hostctl.system._config import SystemConfig

    assert isinstance(_HostConfig.__dict__["scheme"], property)
    # No subclass in this family replaces the property with a plain attribute.
    for config_type in (SystemConfig, PosixConfig, WindowsConfig, IOSConfig):
        assert "scheme" not in config_type.__dict__

    # The URI is built from `uri_scheme` and `scheme` reads it back, so the
    # two can never disagree.
    for config_type, expected in (
        (PosixConfig, "posix"),
        (WindowsConfig, "windows"),
        (IOSConfig, "ios"),
    ):
        config = config_type("node")
        assert config.uri_scheme == expected
        assert config.connection_uri == f"{expected}://node"
        assert config.scheme == expected


def test_config_less_system_host_builds_its_own_family_configuration():
    """A host built without a config gets its family's config, not a mutated base."""
    from hostctl.system._kinds import IOSConfig, PosixConfig, WindowsConfig

    for host_type, config_type, scheme in (
        (PosixHost, PosixConfig, "posix"),
        (WindowsHost, WindowsConfig, "windows"),
        (IOSHost, IOSConfig, "ios"),
    ):
        host = host_type()
        assert type(host.config) is config_type
        assert host.config.scheme == scheme


def test_advertised_system_uris_round_trip_through_hostconfig():
    """Every scheme a config advertises must parse back into a config."""
    from hostctl.host._config import HostConfig
    from hostctl.system._kinds import IOSConfig, PosixConfig, WindowsConfig

    for config_type in (PosixConfig, WindowsConfig, IOSConfig):
        uri = config_type("node").connection_uri
        assert HostConfig(uri).connection_uri == uri


def test_an_embedded_env_value_is_masked_in_an_executor_error_but_still_sent():
    sent = []

    def fail(command, *args, **options):
        sent.extend((command, *args))
        raise subprocess.CalledProcessError(3, command)

    host = PosixHost(
        executor_providers=(ExecutorProvider("fake", fail, capabilities=("args",)),)
    )
    with pytest.raises(subprocess.CalledProcessError) as caught:
        host.run("true", env={"API_TOKEN": "dummy-not-a-secret"})

    assert "dummy-not-a-secret" in " ".join(map(str, sent))
    shown = str(caught.value) + repr(caught.value) + str(caught.value.cmd)
    assert "dummy-not-a-secret" not in shown
    assert "<redacted>" in shown
