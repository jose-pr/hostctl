"""`SSHExecutorProvider.connection`: the asyncssh connection a host runs on."""

from __future__ import annotations

import asyncio
import threading

import pytest

pytest.importorskip("asyncssh")

from conformance.inprocess_ssh import InProcessSSHServer  # noqa: E402
from hostctl.transports.ssh import SSHExecutorProvider, ssh_providers  # noqa: E402


async def _handler(process):
    process.stdout.write(b"ok\n")
    process.exit(0)


def _logins(server):
    return len([event for event in server.events if event[0] == "begin_auth"])


def test_the_connection_opens_on_first_access_and_is_reused():
    with InProcessSSHServer(_handler) as server:
        provider, _ = ssh_providers(server.config())
        try:
            assert _logins(server) == 0
            connection = provider.connection
            assert _logins(server) == 1
            assert provider.connection is connection
            assert _logins(server) == 1
        finally:
            provider.close()


def test_the_connection_runs_coroutines_on_the_provider_loop():
    async def on_loop(connection):
        result = await connection.run("probe", check=True, encoding=None)
        return threading.current_thread().name, result.stdout

    with InProcessSSHServer(_handler) as server:
        provider, _ = ssh_providers(server.config())
        try:
            future = asyncio.run_coroutine_threadsafe(
                on_loop(provider.connection), provider.loop
            )
            thread, output = future.result(timeout=30)
        finally:
            provider.close()

    assert thread == "hostctl-asyncssh-loop"
    assert output == b"ok\n"


def test_close_closes_the_connection_and_the_next_access_reopens():
    with InProcessSSHServer(_handler) as server:
        host = server.config()._create_host()
        try:
            (provider,) = [
                item
                for item in host.executor_providers
                if isinstance(item, SSHExecutorProvider)
            ]
            first = provider.connection
            assert host.run("x").stdout == b"ok\n"
            assert provider.connection is first

            host.close()

            assert first.is_closed()
            # Reopened outside the host's lifecycle: the provider closes it.
            second = provider.connection
            assert second is not first
            assert not second.is_closed()
            assert _logins(server) == 2
            provider.close()
            assert second.is_closed()
        finally:
            host.close()
            provider.close()
