"""SshConfig: no root default, an opt-in ssh_config, connect_options, path_flavor="auto"."""

from __future__ import annotations

import getpass
import socket
import sys

import pytest

pytest.importorskip("asyncssh")

from hostctl import HostConfig, SshConfig
from hostctl.host import PosixHost, WindowsHost
from conformance.inprocess_ssh import InProcessSSHServer


async def _handler(process):
    process.exit(0)


def _causes(exc):
    while exc is not None:
        yield exc
        exc = exc.__cause__ or exc.__context__


def test_without_a_username_the_local_account_is_used():
    config = SshConfig("h")
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
    assert sorted(SshConfig("h").connect_opts()) == [
        "config",
        "connect_timeout",
        "known_hosts",
    ]


def test_ssh_config_is_an_opt_in_on_both_legs(tmp_path, monkeypatch):
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
    assert len(begun) == 2  # one authentication per leg
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
        SshConfig("h", connect_options={key: "x"})
    with pytest.raises(ValueError, match="ssh_config"):
        SshConfig("h", connect_options={"config": None})
    with pytest.raises(ValueError, match="connect_timeout"):
        SshConfig("h", connect_options={"connect_timeout": 1})


def test_connect_options_reach_both_legs_and_stay_out_of_the_repr_and_uri():
    config = SshConfig(
        "h", connect_options={"keepalive_interval": 5, "passphrase": "s3"}
    )

    options = config.connect_opts()
    assert options["keepalive_interval"] == 5

    from hostctl.host._ssh import _SshTransport

    transport = _SshTransport(config)
    transport.path("/x")
    assert transport._sftp_backend.connect_opts["keepalive_interval"] == 5
    assert "s3" not in repr(config) and "s3" not in config.connection_uri


def _sftp_answering(home):
    import asyncssh

    class _Sftp(asyncssh.SFTPServer):
        def realpath(self, path):
            return home.encode() if path == b"." else super().realpath(path)

    return lambda chan: _Sftp(chan)


@pytest.mark.parametrize(
    "answer, kind",
    (("/C:/Users/tester", WindowsHost), ("/home/tester", PosixHost)),
)
def test_auto_path_flavor_asks_the_server_when_the_host_is_created(answer, kind):
    with InProcessSSHServer(_handler, sftp_factory=_sftp_answering(answer)) as server:
        config = server.config(path_flavor="auto")
        host = config._create_host()
        try:
            assert isinstance(host, kind)
        finally:
            host.close()
    assert HostConfig(config.connection_uri).path_flavor == "auto"


def test_auto_path_flavor_refuses_an_answer_it_cannot_classify():
    with InProcessSSHServer(_handler, sftp_factory=_sftp_answering("odd")) as server:
        with pytest.raises(ValueError, match="path style"):
            server.config(path_flavor="auto")._create_host()


def test_a_root_default_is_gone_from_the_source():
    import pathlib

    import hostctl.host._ssh as ssh
    import hostctl.host.qemu as qemu

    for module in (ssh, qemu):
        assert '"root"' not in pathlib.Path(module.__file__).read_text()


def test_an_unresolvable_host_in_auto_mode_cleans_up(monkeypatch):
    with pytest.raises(Exception) as raised:
        SshConfig(
            "hostctl-alias.invalid", path_flavor="auto", known_hosts=None
        )._create_host()
    assert any(isinstance(c, socket.gaierror) for c in _causes(raised.value))
