"""Neither SSH leg reads an ssh_config unless asked to."""

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


def test_an_ssh_config_alias_reaches_neither_leg(tmp_path, monkeypatch):
    with InProcessSSHServer(_handler) as server:
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "config").write_text(
            "Host hostctl-alias.invalid\n"
            "    HostName 127.0.0.1\n"
            f"    Port {server.port}\n"
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        host = server.config(host="hostctl-alias.invalid")._create_host()
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
