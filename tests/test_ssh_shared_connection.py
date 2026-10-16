"""An SSH host's commands and SFTP paths share one connection and one login."""

from __future__ import annotations

import asyncio
import pickle
import socket
import threading
import time

import pytest

pytest.importorskip("asyncssh")

from conformance.inprocess_ssh import InProcessSSHServer  # noqa: E402
from hostctl import Timeout, _async  # noqa: E402
from hostctl.exceptions import OperationNotStartedError  # noqa: E402
from hostctl.transports.ssh import SSHConfig, SSHExecutorProvider  # noqa: E402
from hostctl.transports.ssh._transport import _SSHTransport  # noqa: E402


async def _exit_zero(process):
    process.exit(0)


def _count(server, kind):
    return len([event for event in server.events if event[0] == kind])


def _provider(host) -> SSHExecutorProvider:
    return next(
        item
        for item in host.executor_providers
        if isinstance(item, SSHExecutorProvider)
    )


def _sftppath_loops():
    return [t for t in threading.enumerate() if t.name == "sftppath-asyncssh-loop"]


@pytest.fixture
def server(tmp_path):
    with InProcessSSHServer(_exit_zero, sftp_root=tmp_path) as running:
        yield running


@pytest.mark.parametrize("paths_first", (False, True), ids=("run-first", "path-first"))
def test_run_and_paths_authenticate_once(server, tmp_path, paths_first):
    loops_before = _sftppath_loops()
    host = server.config()._create_host()
    try:
        steps = [
            lambda: host.run("true"),
            lambda: (
                host.path("/a.txt").write_text("alpha"),
                list(host.path("/").iterdir()),
            ),
        ]
        for step in reversed(steps) if paths_first else steps:
            step()
        assert host.path("/a.txt").read_text() == "alpha"
    finally:
        host.close()

    assert _count(server, "connection") == 1
    assert _count(server, "begin_auth") == 1
    assert (tmp_path / "a.txt").read_text() == "alpha"
    # The SFTP requests ran on hostctl's loop: sftppath started none of its own.
    if not loops_before:
        assert _sftppath_loops() == []


def test_the_connection_accessor_is_the_one_the_paths_use(server):
    host = server.config()._create_host()
    try:
        host.path("/").exists()
        transport = _provider(host).transport
        assert transport._sftp_generation[0] is _provider(host).connection
        assert _count(server, "begin_auth") == 1
    finally:
        host.close()


def test_paths_reconnect_after_close_with_one_login_per_connection(server, tmp_path):
    host = server.config()._create_host()
    try:
        before = host.path("/kept.txt")
        before.write_text("one")
        host.run("true")
        host.close()

        # A path handed out before close() runs on the next connection.
        assert before.read_text() == "one"
        host.path("/new.txt").write_text("two")
        host.run("true")
    finally:
        host.close()

    assert _count(server, "connection") == 2
    assert _count(server, "begin_auth") == 2
    assert (tmp_path / "new.txt").read_text() == "two"


def test_a_connection_closed_behind_the_hosts_back_gets_a_new_sftp_channel(
    server, tmp_path
):
    host = server.config()._create_host()
    try:
        path = host.path("/drop.txt")
        path.write_text("before")
        provider = _provider(host)
        connection = provider.connection

        async def drop():
            connection.close()
            await connection.wait_closed()

        asyncio.run_coroutine_threadsafe(drop(), provider.loop).result(30)

        path.write_text("after")
        assert provider.connection is not connection
        assert provider.transport._sftp_generation[0] is provider.connection
    finally:
        host.close()

    assert (tmp_path / "drop.txt").read_text() == "after"
    assert _count(server, "begin_auth") == 2


def test_closing_the_backend_keeps_the_connection(server):
    host = server.config()._create_host()
    try:
        path = host.path("/c.txt")
        path.write_text("x")
        connection = _provider(host).connection
        _provider(host).transport._backend().close()

        assert not connection.is_closed()
        assert path.read_text() == "x"
        assert _provider(host).connection is connection
    finally:
        host.close()

    assert _count(server, "begin_auth") == 1


def test_the_backend_cannot_be_pickled():
    backend = _SSHTransport(SSHConfig("h")).path("/x").backend

    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(backend)


def test_a_failed_connect_declines_the_path_provider(monkeypatch):
    host = SSHConfig("h", verify=False)._create_host()
    transport = _provider(host).transport

    def refuse():
        raise TimeoutError("connect timed out")

    monkeypatch.setattr(transport, "connect", refuse)
    with pytest.raises(OperationNotStartedError) as raised:
        host.path("/x").exists()

    assert isinstance(raised.value.cause, TimeoutError)


def test_the_connect_timeout_bounds_the_first_path_operation():
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
        host = SSHConfig(
            "127.0.0.1",
            port=listener.getsockname()[1],
            timeout=Timeout(connect=0.5),
            verify=False,
        )._create_host()
        started = time.monotonic()
        with pytest.raises(OperationNotStartedError) as raised:
            host.path("/x").exists()
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        thread.join(2)
        for peer in accepted:
            peer.close()
        listener.close()

    assert elapsed < 3
    assert isinstance(raised.value.cause, TimeoutError)


def test_a_forked_child_forgets_the_parents_sftp_channel(monkeypatch):
    class _Channel:
        closed = False

        def close(self):
            self.closed = True

    transport = _SSHTransport(SSHConfig("h"))
    channel = _Channel()
    transport._sftp_generation = (object(), channel)
    transport._loop_generation = _async.loop_generation()
    monkeypatch.setattr(_async, "loop_generation", lambda: -1)

    transport._close_sftp_channel()

    assert transport._sftp_generation is None
    assert channel.closed is False
