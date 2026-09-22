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

from hostctl import _async

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

    # The injected type itself, not "something went wrong": a bare
    # `Exception` also accepts a failure the code should never produce --
    # an AttributeError inside the dispatcher would have passed this.
    with pytest.raises(type(fault())):
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


# --- (f) an unsupported operation does not take the provider out of service --


def test_one_unsupported_operation_does_not_disable_the_provider():
    """`NotImplementedError` scopes to the call, not to the provider.

    A backend that cannot do one derived operation -- `samefile()` needs
    `st_dev`/`st_ino`, which plenty of remote stats lack -- used to be
    declined on the host's shared selector, so the *next* operation on any
    path of that host failed with "no path provider supports open_read".
    """

    class PartialPath(MemPath):
        def samefile(self, other):
            raise NotImplementedError("samefile() needs st_dev/st_ino")

    backend = MemPathBackend()
    MemPath("payload", backend=backend).write_bytes(b"content")
    provider = PathProvider(
        "partial", lambda *parts: PartialPath(*parts, backend=backend)
    )
    host = PosixHost(path_providers=(provider,))

    path = host.path("payload")
    with pytest.raises(NotImplementedError):
        path.samefile(host.path("payload"))

    # The provider is still usable for everything it does implement.
    assert path.read_bytes() == b"content"
    assert host.path("payload").stat().st_size == 7
    assert host.provider_details[0]["availability"] == "available"


# --- (g) the capability vocabulary matches what backends actually do --------


def test_open_write_is_refused_by_a_read_only_provider():
    """A bare `open` granted every `open_*`, `open_write` included.

    So `DownloadPathProvider` -- read operations only -- accepted `open('wb')`
    and was even preferred for it over a writable sibling, which is exactly
    the silent routing of a mutation that operation-level capabilities exist
    to prevent.
    """
    writable_backend = MemPathBackend()
    download = DownloadPathProvider(
        lambda *parts: MemPath(*parts, backend=MemPathBackend())
    )
    writable = PathProvider(
        "sftp", lambda *parts: MemPath(*parts, backend=writable_backend)
    )
    host = PosixHost(path_providers=(download, writable))

    with host.path("payload").open("wb") as stream:
        stream.write(b"data")

    assert MemPath("payload", backend=writable_backend).read_bytes() == b"data"
    assert "open_write" not in download.capabilities


def test_a_read_only_provider_alone_refuses_open_write():
    download = DownloadPathProvider(
        lambda *parts: MemPath(*parts, backend=MemPathBackend())
    )
    host = PosixHost(path_providers=(download,))

    with pytest.raises(NotImplementedError, match="open_write"):
        host.path("payload").open("wb")


def test_a_read_only_provider_still_serves_reads_declared_only_as_open():
    """`open` alone still stands in for `open_read`."""
    backend = MemPathBackend()
    MemPath("payload", backend=backend).write_bytes(b"content")
    provider = PathProvider(
        "legacy",
        lambda *parts: MemPath(*parts, backend=backend),
        capabilities=("open", "stat", "exists"),
    )
    host = PosixHost(path_providers=(provider,))

    with host.path("payload").open("rb") as stream:
        assert stream.read() == b"content"


def test_the_shipped_local_provider_can_symlink_and_readlink(tmp_path):
    """Both were missing from the capability vocabulary.

    A provider that enumerates its operations reported "no path provider
    supports symlink_to" however capable its backend was, while SFTP and
    WinRM escaped only by declaring the `path` wildcard.
    """
    from pathlib_next import Path as LocalPath

    host = PosixHost(path_providers=(LocalPathProvider(lambda *p: LocalPath(*p)),))
    target = host.path(str(tmp_path / "release-42"))
    target.mkdir()
    link = host.path(str(tmp_path / "current"))

    link.symlink_to(str(target))

    assert "release-42" in str(link.readlink())
    assert {"symlink_to", "readlink"} <= PathProvider.DEFAULT_CAPABILITIES


def test_a_dns_failure_declines_to_the_next_provider():
    """A pre-dispatch OSError that is not a ConnectionError still declines.

    `socket.gaierror` (DNS) and EHOSTUNREACH/ENETUNREACH (routing) are OSError
    but not ConnectionError, and asyncssh lets them through untouched, so they
    escaped the provider: ordered fallback stopped dead and a raw socket error
    crossed the public boundary.
    """
    import socket

    from hostctl import SshConfig
    from hostctl.host._ssh import SshExecutorProvider, _SshTransport

    transport = _SshTransport(SshConfig("no-such-host.invalid", username="root"))

    def unresolvable():
        raise socket.gaierror(11001, "getaddrinfo failed")

    transport.connect = unresolvable
    fallback = ExecutorProvider(
        "local",
        lambda command, *args, **options: subprocess.CompletedProcess(
            (command,), 0, b"ok", b""
        ),
    )
    host = PosixHost(executor_providers=(SshExecutorProvider(transport), fallback))

    assert host.run("uptime", check=False).stdout == b"ok"


def test_a_routing_failure_declines_to_the_next_provider():
    import errno

    from hostctl import SshConfig
    from hostctl.host._ssh import SshExecutorProvider, _SshTransport

    transport = _SshTransport(SshConfig("10.0.0.1", username="root"))

    def unreachable():
        raise OSError(errno.EHOSTUNREACH, "No route to host")

    transport.connect = unreachable
    fallback = ExecutorProvider(
        "local",
        lambda command, *args, **options: subprocess.CompletedProcess(
            (command,), 0, b"ok", b""
        ),
    )
    host = PosixHost(executor_providers=(SshExecutorProvider(transport), fallback))

    assert host.run("uptime", check=False).stdout == b"ok"


def test_a_connect_timeout_declines_to_the_next_provider():
    """asyncssh raises a bare `asyncio.TimeoutError` for a connect or login
    timeout, and on 3.9/3.10 -- 3.9 is the declared floor -- that is not the
    builtin `TimeoutError`, so it matched neither the error normalizer nor
    the provider's decline clause. `ConnectTimeout 5` in a user's ssh config
    against a firewalled host therefore escaped `host.run()` verbatim and the
    fallback never ran."""
    import asyncio

    from hostctl import SshConfig
    from hostctl.host._ssh import SshExecutorProvider, _SshTransport

    transport = _SshTransport(SshConfig("firewalled.example", username="root"))

    def timed_out():
        raise _async.normalize_asyncssh_error(asyncio.TimeoutError("timed out"))

    transport.connect = timed_out
    fallback = ExecutorProvider(
        "local",
        lambda command, *args, **options: subprocess.CompletedProcess(
            (command,), 0, b"ok", b""
        ),
    )
    host = PosixHost(executor_providers=(SshExecutorProvider(transport), fallback))

    assert host.run("uptime", check=False).stdout == b"ok"


def test_a_transient_refusal_does_not_brick_the_host():
    """A decline lasts for the call that saw it, not for the selector's life.

    An SSH host has one executor provider, so a single transient refusal --
    sshd restarting, a network blip -- meant hostctl never dialled out again
    until close(), while `host.connect()` still reported success.
    """
    state = {"failing": True}

    def flaky(command, *args, **options):
        if state["failing"]:
            raise OperationNotStarted(
                "sshd restarting", cause=ConnectionError("sshd restarting")
            )
        return subprocess.CompletedProcess((command,), 0, b"ok", b"")

    host = PosixHost(
        executor_providers=(ExecutorProvider("ssh", flaky, capabilities=()),)
    )

    with pytest.raises(OperationNotStarted):
        host.run("uptime", check=False)

    state["failing"] = False
    assert host.run("uptime", check=False).stdout == b"ok"


def test_the_refusal_names_the_provider_and_keeps_its_cause():
    """It surfaced as a bare "no provider is available" with no __cause__."""

    def refuse(command, *args, **options):
        raise OperationNotStarted(
            "host key is not trusted", cause=ConnectionError("host key is not trusted")
        )

    host = PosixHost(
        executor_providers=(ExecutorProvider("ssh", refuse, capabilities=()),)
    )

    with pytest.raises(OperationNotStarted) as raised:
        host.run("uptime", check=False)

    assert "ssh" in str(raised.value)
    assert "host key is not trusted" in str(raised.value)
    assert raised.value.cause is not None
    assert raised.value.__cause__ is not None


def test_path_falls_back_over_every_candidate_not_just_one():
    """`run()` loops over every provider; `path()` fell back exactly once, so
    three ordered providers with the first two offline failed on a host where
    the identical `run()` succeeded."""
    from hostctl import PosixHost
    from hostctl.provider import OperationNotStarted, PathProvider
    from pathlib_next import Path as LocalPath

    def offline(name):
        def factory(*segments):
            raise OperationNotStarted(f"{name} offline")

        return PathProvider(name, factory)

    host = PosixHost(
        path_providers=(
            offline("first"),
            offline("second"),
            PathProvider("third", lambda *parts: LocalPath(*parts)),
        )
    )

    path = host.path("/etc/hosts")

    assert path.provider.name == "third"


def _served_by_memory(text="served"):
    backend = MemPathBackend()
    MemPath("/etc", backend=backend).mkdir(parents=True)
    MemPath("/etc/hostname", backend=backend).write_text(text)
    return PathProvider("memory", lambda *parts: MemPath(*parts, backend=backend))


def _sftp_that_cannot_connect(*, connect=None, warm=None):
    from hostctl import SshConfig
    from hostctl.host._ssh import SftpPathProvider, _SshTransport

    transport = _SshTransport(SshConfig("nas.example", username="root"))
    transport.connect = connect or (lambda: None)
    transport.warm_sftp = warm or (lambda: None)
    return SftpPathProvider(transport)


@pytest.mark.parametrize(
    "error",
    (
        # What `_SshTransport.connect()` makes of each: an untrusted host key
        # (`HostKeyNotVerifiable`) and a refused or firewalled port.
        ConnectionError("Host key is not trusted for host nas.example"),
        ConnectionRefusedError(10061, "connection refused"),
        TimeoutError("connect timed out"),
    ),
    ids=("host-key", "refused", "timeout"),
)
def test_a_path_falls_back_when_sftp_cannot_connect(error):
    """`run()` connects before dispatch and fell back; `path()` did not.

    Nothing on the composite-path route called `SftpPathProvider.connect()`:
    pathlib_next dialled SFTP lazily inside the operation, so the transport
    error escaped `_dispatch` and the working next provider was never tried.
    """

    def refuse():
        raise error

    host = PosixHost(
        path_providers=(_sftp_that_cannot_connect(connect=refuse), _served_by_memory())
    )

    path = host.path("/etc/hostname")

    assert path.read_text() == "served"


def test_a_raw_asyncssh_host_key_error_from_the_sftp_leg_declines():
    """pathlib_next dials its own SFTP connection and raises asyncssh's error
    raw; `HostKeyNotVerifiable` is not an OSError, so it was never taken as
    "nothing started" even where `connect()` did run."""
    import asyncssh

    def untrusted():
        raise asyncssh.HostKeyNotVerifiable("Host key is not trusted")

    host = PosixHost(
        path_providers=(_sftp_that_cannot_connect(warm=untrusted), _served_by_memory())
    )

    assert host.path("/etc/hostname").read_text() == "served"


def test_a_path_refusal_names_its_cause_when_nothing_is_left():
    def refuse():
        raise ConnectionError("Host key is not trusted for host nas.example")

    host = PosixHost(path_providers=(_sftp_that_cannot_connect(connect=refuse),))

    with pytest.raises(OperationNotStarted) as raised:
        host.path("/etc/hostname").read_text()

    assert "Host key is not trusted" in str(raised.value)
    assert isinstance(raised.value.cause, ConnectionError)


def test_a_path_refusal_does_not_brick_a_single_provider_host():
    """Now that a failed SFTP connect declines, the decline must not outlive
    the refusal on a host with nothing to fall back to."""
    state = {"failing": True}

    def flaky():
        if state["failing"]:
            raise ConnectionRefusedError(10061, "sshd restarting")

    backend = MemPathBackend()
    MemPath("/etc", backend=backend).mkdir(parents=True)
    MemPath("/etc/hostname", backend=backend).write_text("back")
    provider = _sftp_that_cannot_connect(connect=flaky)
    provider.factory = lambda *parts: MemPath(*parts, backend=backend)
    host = PosixHost(path_providers=(provider,))

    with pytest.raises(OperationNotStarted):
        host.path("/etc/hostname").read_text()

    state["failing"] = False
    assert host.path("/etc/hostname").read_text() == "back"


def test_a_declined_primary_is_not_redialled_while_the_fallback_serves():
    """Every `run()` cleared every decline, so a dead primary was dialled
    again on each call -- ~9 s apiece against a refused port -- although the
    fallback had been serving all along."""
    dialled = []

    def refuse(command, *args, **options):
        dialled.append(command)
        raise OperationNotStarted(
            "connection refused", cause=ConnectionRefusedError("refused")
        )

    host = PosixHost(
        executor_providers=(
            ExecutorProvider("ssh", refuse, capabilities=()),
            ExecutorProvider(
                "fallback",
                lambda command, *args, **options: subprocess.CompletedProcess(
                    (command,), 0, b"ok", b""
                ),
                capabilities=(),
            ),
        )
    )

    for _ in range(3):
        assert host.run("uptime", check=False).stdout == b"ok"

    assert len(dialled) == 1


def test_two_threads_selecting_do_not_share_a_trace():
    """The per-operation trace and the probe/decline maps were unsynchronised
    instance state, so concurrent operations on one host mixed trace entries
    -- `last_selection` and `path.selection_trace` naming providers the
    caller never tried -- and `select()` could raise "dictionary changed size
    during iteration"."""
    import sys
    import threading

    from hostctl.provider import ProviderSelector

    providers = tuple(
        ExecutorProvider(f"p{index}", lambda *a, **k: None) for index in range(6)
    )
    selector = ProviderSelector(providers)
    errors = []
    mixed = []

    def worker(index):
        name = f"p{index}"
        for _ in range(500):
            try:
                selector.select(exclude=())
                selector.select(exclude=[p.name for p in providers if p.name != name])
                seen = {entry["provider"] for entry in selector.trace}
            except Exception as exc:  # pragma: no cover - the regression
                errors.append(exc)
                return
            if not seen <= {p.name for p in providers}:
                mixed.append(seen)

    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
    finally:
        sys.setswitchinterval(previous)

    assert not errors, errors[:3]
    assert not mixed
