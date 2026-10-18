"""`ssh_config=None` keeps the user's ~/.ssh/config out of both SSH legs."""

from __future__ import annotations

import socket

import pytest

pytest.importorskip("asyncssh")

from conformance.inprocess_ssh import InProcessSSHServer  # noqa: E402


async def _handler(process):
    process.exit(0)


def _causes(exc):
    while exc is not None:
        yield exc
        exc = exc.__cause__ or exc.__context__


def test_with_none_an_alias_reaches_neither_leg(fake_home):
    with InProcessSSHServer(_handler) as server:
        (fake_home / ".ssh" / "config").write_text(
            "Host hostctl-alias.invalid\n"
            "    HostName 127.0.0.1\n"
            f"    Port {server.port}\n"
        )
        host = server.config(
            host="hostctl-alias.invalid",
            ssh_config=None,
            connect_options={"agent_path": None},
        )._create_host()
        try:
            with pytest.raises(Exception) as run_error:
                host.run("x")
            with pytest.raises(Exception) as path_error:
                host.path("/").exists()
        finally:
            host.close()

    for caught in (run_error, path_error):
        assert any(
            isinstance(cause, socket.gaierror) for cause in _causes(caught.value)
        ), caught.value
    assert not [event for event in server.events if event[0] == "begin_auth"]
