"""One transport per host, a closed host that stays closed, and deadlines
that tell a wedged agent from a long command."""

from __future__ import annotations

import subprocess
import threading

import pytest

from hostctl.executor._qga import QgaProtocolError
from hostctl.executor.qemu import GuestAgentProtocolError, QemuExecutor
from hostctl.host import HostConfig
from hostctl.host.qemu import QemuConfig, QemuHost


class _Transport:
    """A guest agent that answers the discovery handshake."""

    instances = 0

    def __init__(self, *, slow: bool = False):
        type(self).instances += 1
        self.closed = 0
        self.slow = slow
        self.requests = []

    def execute(self, request, timeout=None):
        self.requests.append((request["execute"], timeout))
        if self.slow:
            # Hold the first caller long enough for a second thread to see
            # an unbuilt transport and start building its own.
            self.slow = False
            threading.Event().wait(0.2)
        if request["execute"] == "guest-info":
            return {
                "supported_commands": [
                    {"name": name, "enabled": True}
                    for name in (
                        "guest-file-open",
                        "guest-file-read",
                        "guest-file-write",
                        "guest-file-close",
                    )
                ]
            }
        return {}

    def close(self):
        self.closed += 1


def _host(*, building_takes=0.0, **kwargs):
    made = []

    def factory():
        # Building a real transport is not instant -- the SSH flavour opens
        # a connection here -- so the window two threads race through is
        # inside the factory, not around it.
        if building_takes:
            threading.Event().wait(building_takes)
        transport = _Transport(**kwargs)
        made.append(transport)
        return transport

    # `path_flavor` is explicit because this agent advertises no
    # `guest-get-osinfo`: the host refuses to guess a family, which is the
    # subject of its own test.
    config = QemuConfig("guest", transport_factory=factory, path_flavor="posix")
    return QemuHost(config), made


def test_concurrent_first_use_builds_one_transport():
    """Lazy initialisation was unguarded.

    Two threads each built a transport; for the SSH flavour each also
    opened its own SSH connection, and the loser's was dropped still open
    -- a connection nobody owned, which `close()` could not reach.
    """
    host, made = _host(building_takes=0.2)
    seen = []

    def use():
        seen.append(host.transport)

    threads = [threading.Thread(target=use) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(made) == 1
    assert {id(value) for value in seen} == {id(made[0])}


def test_a_path_from_a_closed_host_refuses_instead_of_reconnecting():
    """`close()` dropped the host's reference and nothing else.

    A path handed out earlier still held the backend, the backend still
    held the transport, and the framed transports reconnect transparently
    -- so the next operation on a stale path silently opened a connection
    nobody owned.
    """
    host, _ = _host()
    path = host.path("/etc/hostname")

    host.close()

    with pytest.raises(ValueError, match="closed"):
        path.read_bytes()


def test_the_reply_cap_is_configurable_and_round_trips():
    """8 MiB was hard-wired, below what qemu-ga itself will capture."""
    config = QemuConfig("guest", max_reply_size=64 * 1024 * 1024)
    assert config.max_reply_size == 64 * 1024 * 1024

    restored = HostConfig(config.connection_uri)
    assert isinstance(restored, QemuConfig)
    assert restored.max_reply_size == 64 * 1024 * 1024

    with pytest.raises(ValueError, match="max_reply_size"):
        QemuConfig("guest", max_reply_size=0)


class _WedgedTransport:
    """An agent that accepts `guest-exec` and then stops answering."""

    def __init__(self):
        self.timeouts = []

    def execute(self, request, timeout=None):
        self.timeouts.append(timeout)
        if request["execute"] == "guest-exec":
            return {"pid": 7}
        raise TimeoutError("no reply")


def test_a_wedged_agent_is_not_reported_as_a_command_timeout():
    """The command's own deadline was handed to every RPC.

    `guest-exec-status` answers immediately when the agent is healthy, so a
    request that never returns means a wedged agent -- but with
    `timeout=600` the executor waited the full ten minutes and then called
    it a command timeout.
    """
    transport = _WedgedTransport()
    executor = QemuExecutor(lambda: transport, agent_timeout=0.25, sleep=lambda _: None)

    with pytest.raises(ConnectionError, match="stopped responding"):
        executor("/bin/true", timeout=600)

    assert max(value for value in transport.timeouts if value) <= 0.25


class _SlowCommandTransport:
    """An agent that answers promptly; the command is what takes too long."""

    def __init__(self):
        self.calls = 0

    def execute(self, request, timeout=None):
        self.calls += 1
        if request["execute"] == "guest-exec":
            return {"pid": 9}
        return {"exited": False}


def test_a_long_command_still_times_out_as_a_command():
    clock = iter([0.0, 0.0, 0.0, 0.2, 5.0, 5.0, 5.0])
    transport = _SlowCommandTransport()
    executor = QemuExecutor(
        lambda: transport,
        agent_timeout=30.0,
        clock=lambda: next(clock),
        sleep=lambda _: None,
    )

    with pytest.raises(subprocess.TimeoutExpired):
        executor("/bin/sleep", "10", timeout=1)


class _OversizedTransport:
    def execute(self, request, timeout=None):
        if request["execute"] == "guest-exec":
            return {"pid": 11}
        raise QgaProtocolError("QGA reply exceeded size limit")


def test_a_rejected_status_reply_names_the_process_and_the_setting():
    """The output is already gone; the bare protocol error said nothing."""
    executor = QemuExecutor(lambda: _OversizedTransport(), sleep=lambda _: None)

    with pytest.raises(GuestAgentProtocolError) as caught:
        executor("/bin/cat", "/dev/urandom")

    assert "pid 11" in str(caught.value)
    assert "max_reply_size" in str(caught.value)
