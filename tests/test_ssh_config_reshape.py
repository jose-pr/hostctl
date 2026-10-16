"""SSHConfig: no root default, an opt-in ssh_config, connect_options, pathname="auto"."""

from __future__ import annotations

import getpass
import socket
import sys

import pytest

pytest.importorskip("asyncssh")

from hostctl import Timeout
from hostctl.host._config import HostConfig
from hostctl.transports.ssh._config import SSHConfig
from hostctl.system._kinds import PosixHost, WindowsHost
from conformance.inprocess_ssh import InProcessSSHServer


async def _handler(process):
    process.exit(0)


def _causes(exc):
    while exc is not None:
        yield exc
        exc = exc.__cause__ or exc.__context__


def test_without_a_username_the_local_account_is_used():
    config = SSHConfig("h")
    assert config.username is None
    assert "username" not in config.connect_opts()
    assert "root@" not in config.connection_uri
    assert HostConfig("ssh://h").username is None
    assert HostConfig(config.connection_uri).username is None
    assert HostConfig("ssh://root@h").username == "root"


def test_the_server_sees_the_local_user_when_none_is_given():
    with InProcessSSHServer(_handler) as server:
        host = server.config(username=None)._create_host()
        try:
            host.run("true")
        finally:
            host.close()

    assert ("begin_auth", getpass.getuser()) in server.events


def test_the_default_options_read_no_ssh_config():
    assert sorted(SSHConfig("h").connect_opts()) == [
        "config",
        "connect_timeout",
        "known_hosts",
    ]


def test_ssh_config_is_an_opt_in_for_the_one_connection(tmp_path, monkeypatch):
    with InProcessSSHServer(_handler, sftp_root=tmp_path) as server:
        path = tmp_path / "ssh_config"
        path.write_text(
            "Host hostctl-alias.invalid\n"
            "    HostName 127.0.0.1\n"
            f"    Port {server.port}\n"
        )
        config = server.config(host="hostctl-alias.invalid", ssh_config=path)
        host = config._create_host()
        try:
            assert host.run("true").returncode == 0
            assert host.path("/").exists() in (True, False)  # reaches the server
        finally:
            host.close()

    begun = [event for event in server.events if event[0] == "begin_auth"]
    assert len(begun) == 1  # run() and path() share the alias's connection
    assert "ssh_config" not in config.connection_uri


def test_a_uri_cannot_name_an_ssh_config():
    # A config's ProxyCommand would run a process the URI's author chose.
    with pytest.raises(ValueError, match="ssh_config"):
        HostConfig("ssh://h?ssh_config=/tmp/x")


@pytest.mark.parametrize(
    "key", ("host", "port", "username", "password", "client_keys", "known_hosts")
)
def test_connect_options_cannot_override_what_hostctl_owns(key):
    with pytest.raises(ValueError, match=key):
        SSHConfig("h", connect_options={key: "x"})
    with pytest.raises(ValueError, match="ssh_config"):
        SSHConfig("h", connect_options={"config": None})
    with pytest.raises(ValueError, match=r"timeout=Timeout\(connect=\.\.\.\)"):
        SSHConfig("h", connect_options={"connect_timeout": 1})


def test_connect_options_stay_out_of_the_repr_and_uri():
    config = SSHConfig(
        "h", connect_options={"keepalive_interval": 5, "passphrase": "s3"}
    )

    options = config.connect_opts()
    assert options["keepalive_interval"] == 5
    assert "s3" not in repr(config) and "s3" not in config.connection_uri


def test_connect_options_reach_the_connection_paths_use(tmp_path):
    with InProcessSSHServer(_handler, sftp_root=tmp_path) as server:
        config = server.config(connect_options={"client_version": "hostctl-probe"})
        host = config._create_host()
        try:
            host.path("/marker").write_text("x")
            host.run("true")
        finally:
            host.close()

    connections = [event for event in server.events if event[0] == "connection"]
    assert len(connections) == 1
    assert "hostctl-probe" in connections[0][1].get_extra_info("client_version")


def _sftp_answering(home):
    import asyncssh

    class _Sftp(asyncssh.SFTPServer):
        def realpath(self, path):
            return home.encode() if path == b"." else super().realpath(path)

    return lambda chan: _Sftp(chan)


@pytest.mark.parametrize(
    "answer, kind",
    (("/C:/Users/devuser", WindowsHost), ("/home/tester", PosixHost)),
)
def test_auto_pathname_asks_the_server_when_the_host_is_created(answer, kind):
    with InProcessSSHServer(_handler, sftp_factory=_sftp_answering(answer)) as server:
        config = server.config(pathname="auto")
        host = config._create_host()
        try:
            assert isinstance(host, kind)
        finally:
            host.close()
    assert HostConfig(config.connection_uri).pathname == "auto"


def test_auto_pathname_refuses_an_answer_it_cannot_classify():
    with InProcessSSHServer(_handler, sftp_factory=_sftp_answering("odd")) as server:
        with pytest.raises(ValueError, match="path style"):
            server.config(pathname="auto")._create_host()


def test_a_root_default_is_gone_from_the_source():
    import pathlib

    import hostctl.transports.ssh._transport as ssh
    import hostctl.transports.qemu._host as qemu

    for module in (ssh, qemu):
        assert '"root"' not in pathlib.Path(module.__file__).read_text()


def test_an_unresolvable_host_in_auto_mode_cleans_up(monkeypatch):
    with pytest.raises(Exception) as raised:
        SSHConfig("hostctl-alias.invalid", pathname="auto", verify=False)._create_host()
    assert any(isinstance(c, socket.gaierror) for c in _causes(raised.value))


@pytest.mark.parametrize("value", (None, 5.0, 30.0))
def test_a_connect_timeout_round_trips_through_the_uri(value):
    config = SSHConfig("h", timeout=Timeout(connect=value))
    restored = HostConfig(config.connection_uri)
    assert restored.timeout == Timeout(connect=value)
    assert restored == config
