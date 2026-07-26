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
    ExecutorProvider,
    OperationNotStarted,
    PathProvider,
    PosixHost,
    ProviderProbe,
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
