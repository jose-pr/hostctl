"""run(timeout=) over SSH is bounded whatever the server does with TERM."""

from __future__ import annotations

import asyncio
import subprocess
import time

import pytest

pytest.importorskip("asyncssh")

from conformance.inprocess_ssh import InProcessSSHServer  # noqa: E402


def test_a_server_that_ignores_term_cannot_hold_a_timed_out_run():
    async def handler(process):
        await asyncio.sleep(6)
        process.exit(0)

    with InProcessSSHServer(handler) as server:
        host = server.config()._create_host()
        try:
            started = time.monotonic()
            with pytest.raises(subprocess.TimeoutExpired) as caught:
                host.run("ignore", timeout=1)
            elapsed = time.monotonic() - started
        finally:
            host.close()

    assert elapsed < 4.5
    assert caught.value.orphaned is True


def test_a_command_that_ends_on_term_is_not_orphaned():
    async def handler(process):
        done = asyncio.get_running_loop().create_future()

        def on_signal(name):
            server_events.append(("signal", name))
            if not done.done():
                done.set_result(name)

        process.signal_received = on_signal
        try:
            await asyncio.wait_for(done, 6)
            process.exit(143)
        except asyncio.TimeoutError:
            process.exit(0)

    server_events: list = []
    with InProcessSSHServer(handler) as server:
        server_events = server.events
        host = server.config()._create_host()
        try:
            started = time.monotonic()
            with pytest.raises(subprocess.TimeoutExpired) as caught:
                host.run("honour", timeout=1)
            elapsed = time.monotonic() - started
        finally:
            host.close()

    assert elapsed < 3.5
    assert caught.value.orphaned is False
    assert ("signal", "TERM") in server.events
