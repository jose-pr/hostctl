"""Fault-injection proof of the no-replay safety rule (plan Design Q3).

After DISPATCH, a failed command or path mutation must fail terminally.  A
second provider may only be reached when the first declines *before* anything
could have started: a probe/preflight rejection, or an explicit
``OperationNotStarted``.

Every double here records each invocation, so "the next provider was never
invoked" is asserted against a call ledger rather than inferred from the raised
exception alone.
"""

import subprocess

import pytest
from pathlib_next.mempath import MemPath, MemPathBackend

from hostctl import (
    ContainerConfig,
    ContainerHost,
    ExecutorProvider,
    LocalHost,
    OperationNotStarted,
    PathProvider,
    PosixHost,
    ProviderProbe,
)
from hostctl.provider.transports import (
    ARCHIVE_PATH_OPERATIONS,
    ContainerArchivePathProvider,
    DownloadPathProvider,
    LocalExecutorProvider,
    LocalPathProvider,
)


class FaultInjector:
    """Records provider invocations and injects one fault per provider."""

    def __init__(self):
        self.calls = []

    def executor(self, name, fault=None, **options):
        def execute(command, *args, **kwargs):
            self.calls.append(name)
            if fault is not None:
                raise fault()
            return subprocess.CompletedProcess((command, *args), 0, b"ok", b"")

        return ExecutorProvider(name, execute, **options)

    def path(self, name, backend, fault=None, **options):
        def factory(*segments):
            self.calls.append(name)
            if fault is not None:
                raise fault()
            return MemPath(*segments, backend=backend)

        return PathProvider(name, factory, **options)


# --- (a) a dispatched failure never reaches the next provider ---------------


def test_executor_disconnect_after_dispatch_never_invokes_the_next_provider():
    """A transport that drops mid-command may have already run it."""
    injector = FaultInjector()
    host = PosixHost(
        executor_providers=(
            injector.executor(
                "primary",
                fault=lambda: ConnectionResetError("connection dropped mid-command"),
            ),
            injector.executor("secondary"),
        )
    )

    with pytest.raises(ConnectionResetError):
        host.run("systemctl restart nginx", check=False)

    assert injector.calls == ["primary"]


@pytest.mark.parametrize(
    "fault",
    [
        lambda: ConnectionResetError("dropped"),
        lambda: TimeoutError("no response after dispatch"),
        lambda: OSError("transport went away"),
        lambda: RuntimeError("unknown transport state"),
    ],
    ids=["reset", "timeout", "oserror", "unknown"],
)
def test_no_uncertain_executor_failure_is_ever_replayed(fault):
    """Any failure that is not provably pre-dispatch is terminal."""
    injector = FaultInjector()
    host = PosixHost(
        executor_providers=(
            injector.executor("primary", fault=fault),
            injector.executor("secondary"),
        )
    )

    with pytest.raises(Exception):
        host.run("rm -rf /var/cache/app", check=False)

    assert injector.calls == ["primary"]


def test_uncertain_path_write_never_falls_through_to_the_next_provider():
    """A write whose outcome is unknown must not be retried elsewhere."""
    injector = FaultInjector()
    second_backend = MemPathBackend()
    host = PosixHost(
        path_providers=(
            injector.path(
                "primary",
                MemPathBackend(),
                fault=lambda: ConnectionResetError("dropped during write"),
            ),
            injector.path("secondary", second_backend),
        )
    )

    with pytest.raises(ConnectionResetError):
        host.path("state.db").write_bytes(b"payload")

    assert injector.calls == ["primary"]
    # The untouched provider must not hold a duplicate of the write.
    assert not MemPath("state.db", backend=second_backend).exists()


def test_partial_write_on_a_pinned_stream_is_not_replayed():
    """An open write stream owns its backend until it is closed."""
    injector = FaultInjector()
    primary_backend = MemPathBackend()
    secondary_backend = MemPathBackend()
    host = PosixHost(
        path_providers=(
            injector.path("primary", primary_backend),
            injector.path("secondary", secondary_backend),
        )
    )

    path = host.path("stream.bin")
    with path.open("wb") as stream:
        stream.write(b"half")

    assert path.provider.name == "primary"
    assert path._pinned is True
    assert injector.calls == ["primary"]
    assert not MemPath("stream.bin", backend=secondary_backend).exists()


# --- (b) preflight rejection selects the next provider exactly once ---------


def test_preflight_rejection_selects_the_next_provider_exactly_once():
    """OperationNotStarted proves nothing ran, so fallback is permitted."""
    injector = FaultInjector()
    host = PosixHost(
        executor_providers=(
            injector.executor(
                "primary",
                fault=lambda: OperationNotStarted("refused before dispatch"),
            ),
            injector.executor("secondary"),
        )
    )

    result = host.run("uptime", check=False)

    assert result.stdout == b"ok"
    assert injector.calls == ["primary", "secondary"]


def test_probe_rejection_skips_a_provider_without_invoking_it():
    """A provider declared unavailable is never dispatched to at all."""
    injector = FaultInjector()
    unavailable = injector.executor(
        "primary",
        probe=lambda: ProviderProbe("unavailable", "port closed"),
    )
    host = PosixHost(executor_providers=(unavailable, injector.executor("secondary")))

    result = host.run("uptime", check=False)

    assert result.stdout == b"ok"
    assert injector.calls == ["secondary"]


def test_preflight_rejection_does_not_cascade_past_the_second_provider():
    """Exactly one fallback step happens per declining provider."""
    injector = FaultInjector()
    host = PosixHost(
        executor_providers=(
            injector.executor("first", fault=lambda: OperationNotStarted("declined")),
            injector.executor("second"),
            injector.executor("third"),
        )
    )

    host.run("uptime", check=False)

    assert injector.calls == ["first", "second"]
    assert "third" not in injector.calls


def test_path_preflight_rejection_selects_the_next_provider_exactly_once():
    injector = FaultInjector()
    backend = MemPathBackend()
    MemPath("payload", backend=backend).write_bytes(b"content")
    host = PosixHost(
        path_providers=(
            injector.path(
                "primary",
                MemPathBackend(),
                fault=lambda: OperationNotStarted("SFTP subsystem unavailable"),
            ),
            injector.path("secondary", backend),
            injector.path("tertiary", MemPathBackend()),
        )
    )

    assert host.path("payload").read_bytes() == b"content"
    assert injector.calls == ["primary", "secondary"]


def test_a_decline_is_remembered_for_the_generation_and_cleared_on_reconnect():
    """A provider that declined is not re-attempted until invalidation."""
    injector = FaultInjector()
    backend = MemPathBackend()
    MemPath("payload", backend=backend).write_bytes(b"content")
    declining = injector.path(
        "primary",
        MemPathBackend(),
        fault=lambda: OperationNotStarted("subsystem unavailable"),
    )
    host = PosixHost(
        path_providers=(declining, injector.path("secondary", backend)),
    )

    path = host.path("payload")
    assert injector.calls == ["primary", "secondary"]

    # Later operations must not re-attempt the provider that already declined.
    assert path.read_bytes() == b"content"
    assert path.exists() is True
    assert injector.calls == ["primary", "secondary"]

    # A new generation re-probes everything, including the declined provider.
    host._path_selector.invalidate()
    host.path("payload").read_bytes()
    assert injector.calls == ["primary", "secondary", "primary", "secondary"]


def test_declined_provider_appears_in_the_selection_trace_without_secrets():
    injector = FaultInjector()
    backend = MemPathBackend()
    MemPath("payload", backend=backend).write_bytes(b"content")
    host = PosixHost(
        path_providers=(
            injector.path(
                "primary",
                MemPathBackend(),
                fault=lambda: OperationNotStarted("token=hunter2 rejected"),
            ),
            injector.path("secondary", backend),
        )
    )

    path = host.path("payload")
    path.read_bytes()
    trace = path.selection_trace

    declined = [item for item in trace if item["provider"] == "primary"]
    assert declined and declined[0]["chosen"] is False
    assert "hunter2" not in str(trace)
    assert "<redacted>" in declined[0]["reason"]


def test_dispatched_failure_after_a_declined_provider_is_still_terminal():
    """Fallback does not license a second replay once dispatch happens."""
    injector = FaultInjector()
    host = PosixHost(
        executor_providers=(
            injector.executor("first", fault=lambda: OperationNotStarted("declined")),
            injector.executor(
                "second", fault=lambda: ConnectionResetError("dropped mid-command")
            ),
            injector.executor("third"),
        )
    )

    with pytest.raises(ConnectionResetError):
        host.run("systemctl restart nginx", check=False)

    assert injector.calls == ["first", "second"]


# --- (c) the local assembly obeys the same rule ------------------------------


def test_local_host_is_assembled_from_providers():
    """LocalHost composes providers instead of a hard-wired executor."""
    host = LocalHost()

    assert [provider.name for provider in host.executor_providers] == ["local"]
    assert [provider.name for provider in host.path_providers] == ["local"]
    assert isinstance(host.executor_providers[0], LocalExecutorProvider)
    assert isinstance(host.path_providers[0], LocalPathProvider)
    # The public capability set is unchanged by the assembly.
    assert host.capabilities == frozenset(("run", "path"))


def test_local_assembly_never_replays_a_dispatched_command():
    """A local command that failed after dispatch is terminal."""
    injector = FaultInjector()
    host = LocalHost(
        executor_providers=(
            injector.executor(
                "primary", fault=lambda: ConnectionResetError("dropped mid-command")
            ),
            injector.executor("secondary"),
        )
    )

    with pytest.raises(ConnectionResetError):
        host.run("systemctl restart nginx", check=False)

    assert injector.calls == ["primary"]


def test_local_assembly_falls_back_only_on_a_proven_pre_dispatch_refusal():
    injector = FaultInjector()
    host = LocalHost(
        executor_providers=(
            injector.executor(
                "primary", fault=lambda: OperationNotStarted("refused before dispatch")
            ),
            injector.executor("secondary"),
            injector.executor("tertiary"),
        )
    )

    result = host.run("uptime", check=False)

    assert result.stdout == b"ok"
    assert injector.calls == ["primary", "secondary"]


def test_local_assembly_skips_a_probe_rejected_provider_without_invoking_it():
    injector = FaultInjector()
    host = LocalHost(
        executor_providers=(
            injector.executor(
                "primary", probe=lambda: ProviderProbe("unavailable", "shell missing")
            ),
            injector.executor("secondary"),
        )
    )

    assert host.run("uptime", check=False).stdout == b"ok"
    assert injector.calls == ["secondary"]


def test_local_path_assembly_does_not_replay_an_uncertain_path_build():
    injector = FaultInjector()
    second_backend = MemPathBackend()
    host = LocalHost(
        path_providers=(
            injector.path(
                "local",
                MemPathBackend(),
                fault=lambda: ConnectionResetError("filesystem went away"),
            ),
            injector.path("secondary", second_backend),
        )
    )

    with pytest.raises(ConnectionResetError):
        host.path("state.db")

    assert injector.calls == ["local"]


# --- (d) the container assembly obeys the same rule --------------------------


class _FakeContainer:
    """A container double that never touches Docker or the network."""

    def __init__(self):
        self.attrs = {
            "Id": "fake",
            "Platform": "linux",
            "Architecture": "amd64",
            "State": {"Running": True},
        }

    def reload(self):
        return None

    def exec_run(self, command, **options):
        raise AssertionError("the executor provider double must own dispatch")


class _FakeContainers:
    def __init__(self, container):
        self.container = container

    def get(self, name):
        return self.container


class _FakeClient:
    def __init__(self):
        self.containers = _FakeContainers(_FakeContainer())
        self.closed = False

    def close(self):
        self.closed = True


def _container_host(**providers):
    client = _FakeClient()
    config = ContainerConfig("fake-target", client_factory=lambda **_: client)
    return ContainerHost(config, **providers), client


def test_container_host_is_assembled_from_providers():
    host, _ = _container_host()

    assert [provider.name for provider in host.executor_providers] == ["container"]
    assert [provider.name for provider in host.path_providers] == ["archive"]
    assert isinstance(host.path_providers[0], ContainerArchivePathProvider)
    # The public capability set is unchanged by the assembly.
    assert host.capabilities == frozenset(("run", "path", "spawn", "tty"))


def test_container_archive_provider_declares_no_namespace_mutations():
    """The archive API cannot mkdir/chmod/unlink/rmdir/rename, so it says so."""
    host, _ = _container_host()
    capabilities = host.path_providers[0].capabilities

    assert capabilities == ARCHIVE_PATH_OPERATIONS
    for operation in ("mkdir", "chmod", "unlink", "rmdir", "rename"):
        assert operation not in capabilities
    # Content operations the archive API genuinely implements remain declared.
    for operation in ("read", "write", "stat", "scandir", "open_read", "open_write"):
        assert operation in capabilities


def test_container_assembly_never_replays_a_dispatched_exec():
    injector = FaultInjector()
    host, _ = _container_host(
        executor_providers=(
            injector.executor(
                "primary", fault=lambda: ConnectionResetError("exec stream dropped")
            ),
            injector.executor("secondary"),
        )
    )

    with pytest.raises(ConnectionResetError):
        host.run("systemctl restart nginx", check=False)

    assert injector.calls == ["primary"]


def test_container_assembly_falls_back_once_on_a_pre_dispatch_refusal():
    injector = FaultInjector()
    host, _ = _container_host(
        executor_providers=(
            injector.executor(
                "primary", fault=lambda: OperationNotStarted("container starting")
            ),
            injector.executor("secondary"),
            injector.executor("tertiary"),
        )
    )

    assert host.run("uptime", check=False).stdout == b"ok"
    assert injector.calls == ["primary", "secondary"]


def test_container_uncertain_path_write_is_not_retried_elsewhere():
    injector = FaultInjector()
    second_backend = MemPathBackend()
    host, _ = _container_host(
        path_providers=(
            injector.path(
                "archive",
                MemPathBackend(),
                fault=lambda: ConnectionResetError("archive upload dropped"),
            ),
            injector.path("secondary", second_backend),
        )
    )

    with pytest.raises(ConnectionResetError):
        host.path("/etc/app.conf")

    assert injector.calls == ["archive"]
    assert not MemPath("/etc/app.conf", backend=second_backend).exists()


def test_container_close_releases_the_provider_and_the_sdk_client():
    host, client = _container_host()
    with host:
        pass
    assert client.closed


# --- (e) read-only providers reject mutations instead of falling through -----


def test_read_only_download_provider_rejects_a_mutation_without_fallback():
    """A declared read-only provider must not silently route a write away."""
    injector = FaultInjector()
    writable_backend = MemPathBackend()
    download = DownloadPathProvider(
        lambda *parts: MemPath(*parts, backend=writable_backend)
    )
    host = PosixHost(path_providers=(download,))

    path = host.path("payload")
    with pytest.raises(NotImplementedError):
        path.write_bytes(b"mutation")

    assert "write" not in download.capabilities
    assert not MemPath("payload", backend=writable_backend).exists()
    assert injector.calls == []
