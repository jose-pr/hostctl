"""Connecting, closing and forking an SSH host."""

from __future__ import annotations

import socket
import threading
import time

import pytest

pytest.importorskip("asyncssh")

from hostctl import HostConfig, SshConfig
from hostctl import _async
from hostctl.provider import OperationNotStarted


def _causes(exc):
    while exc is not None:
        yield exc
        exc = exc.__cause__ or exc.__context__


def _closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_connect_timeout_bounds_a_silent_server():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    accepted = []
    stop = threading.Event()

    def accept():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                accepted.append(listener.accept()[0])
            except OSError:
                pass

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        host = SshConfig(
            "127.0.0.1",
            port=listener.getsockname()[1],
            connect_timeout=0.5,
            known_hosts=None,
        ).open()
        started = time.monotonic()
        with pytest.raises(OperationNotStarted) as raised:
            host.connect()
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        thread.join(2)
        for peer in accepted:
            peer.close()
        listener.close()

    assert elapsed < 3
    assert any(isinstance(cause, TimeoutError) for cause in _causes(raised.value))


def test_connect_with_nothing_connected_raises():
    host = SshConfig("127.0.0.1", port=_closed_port(), known_hosts=None).open()

    with pytest.raises(OperationNotStarted, match="no provider connected"):
        host.connect()
    assert host._connected is False
    with pytest.raises(OperationNotStarted):
        with host:
            pass
    # Nothing was remembered: a second attempt tries again.
    with pytest.raises(OperationNotStarted, match="no provider connected"):
        host.connect()


def test_close_in_a_forked_child_leaves_the_parent_connection(monkeypatch):
    from hostctl.host._ssh import _SshTransport

    class _Connection:
        closed = False

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    transport = _SshTransport(SshConfig("h"))
    connection = _Connection()
    transport._ssh = connection
    transport._loop_generation = _async.loop_generation()
    monkeypatch.setattr(_async, "loop_generation", lambda: -1)

    transport.close()

    assert connection.closed is False
    assert transport._ssh is None


def test_connect_timeout_round_trips_the_uri_and_is_validated():
    config = SshConfig("h", connect_timeout=5)
    restored = HostConfig(config.connection_uri)
    assert restored.connect_timeout == 5.0
    assert "connect_timeout" not in SshConfig("h").connection_uri
    assert SshConfig("h").connect_opts()["connect_timeout"] == 30.0
    assert "connect_timeout" not in SshConfig("h", connect_timeout=None).connect_opts()
    with pytest.raises(ValueError, match="connect_timeout"):
        SshConfig("h", connect_timeout=0)


async def _no_command(process):
    process.exit(0)


def test_a_paths_only_host_authenticates_once_and_removes_recursively(tmp_path):
    from conformance.inprocess_ssh import InProcessSSHServer

    (tmp_path / "tree" / "inner").mkdir(parents=True)
    (tmp_path / "tree" / "inner" / "leaf.txt").write_text("x")
    with InProcessSSHServer(_no_command, sftp_root=tmp_path) as server:
        host = server.config()._create_host()
        try:
            # The first SFTP operation is a recursive remove: the client it
            # needs must already be cached on the calling thread.
            host.path("/tree").rm(recursive=True)
        finally:
            host.close()

    assert not (tmp_path / "tree").exists()
    assert [e for e in server.events if e[0] == "begin_auth"] == [
        ("begin_auth", "tester")
    ]
