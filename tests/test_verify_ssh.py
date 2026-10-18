"""`SSHConfig(verify=)`: `False` is the one way to skip the host-key check.

Driven against the in-process asyncssh server, on both legs (the exec
connection and SFTP). A refused host key ends the connection before
authentication, so a refusal also means no credential reached the server.
"""

from __future__ import annotations

import pytest

pytest.importorskip("asyncssh")

from hostctl import HostConfig, QEMUConfig, SSHConfig  # noqa: E402
from conformance.inprocess_ssh import InProcessSSHServer  # noqa: E402


async def _handler(process):
    process.exit(0)


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "sftp"
    root.mkdir()
    with InProcessSSHServer(_handler, sftp_root=root) as running:
        yield running


@pytest.fixture
def empty_home(tmp_path, monkeypatch):
    """A home with no `~/.ssh/known_hosts`, so the usual lookup trusts nothing."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def unrelated_known_hosts(tmp_path, server):
    import asyncssh

    key = asyncssh.generate_private_key("ssh-ed25519")
    public = key.export_public_key("openssh").decode().split()[:2]
    path = tmp_path / "unrelated_known_hosts"
    path.write_text(f"[127.0.0.1]:{server.port} {public[0]} {public[1]}\n")
    return str(path)


def _authenticated(server) -> bool:
    return any(event[0] == "begin_auth" for event in server.events)


def _exec_connects(config) -> bool:
    host = config._create_host()
    try:
        host.run("x")
        return True
    except ConnectionError:
        return False
    finally:
        host.close()


def _sftp_connects(config) -> bool:
    host = config._create_host()
    try:
        host.path("/").exists()
        return True
    except ConnectionError:
        return False
    finally:
        host.close()


@pytest.mark.parametrize("leg", (_exec_connects, _sftp_connects))
def test_the_default_verifies(server, empty_home, leg):
    config = server.config(known_hosts=())
    assert config.verify is True
    assert leg(config) is False
    assert not _authenticated(server)


@pytest.mark.parametrize("leg", (_exec_connects, _sftp_connects))
def test_false_skips_the_usual_lookup(server, empty_home, leg):
    assert leg(server.config(known_hosts=(), verify=False)) is True


@pytest.mark.parametrize("leg", (_exec_connects, _sftp_connects))
def test_a_named_known_hosts_is_checked_even_under_false(
    server, unrelated_known_hosts, leg
):
    # The stricter setting wins: naming the trusted keys is a request to check.
    assert leg(server.config(known_hosts=unrelated_known_hosts, verify=False)) is False
    assert not _authenticated(server)
    assert leg(server.config(verify=False)) is True


@pytest.mark.parametrize("leg", (_exec_connects, _sftp_connects))
def test_only_false_switches_the_check_off(server, unrelated_known_hosts, leg):
    for known_hosts in ((), [], b"", unrelated_known_hosts, [unrelated_known_hosts]):
        assert leg(server.config(known_hosts=known_hosts)) is False, known_hosts
    assert not _authenticated(server)


def test_known_hosts_none_is_refused():
    with pytest.raises(ValueError, match="verify=False"):
        SSHConfig("nas.example", known_hosts=None)
    with pytest.raises(ValueError, match="verify=False"):
        HostConfig("ssh://root@nas.example", known_hosts=None)


@pytest.mark.parametrize("value", ("/etc/ssl/ca.pem", "false", None, 0, 1))
def test_verify_is_a_bool(value):
    with pytest.raises(TypeError, match="verify"):
        SSHConfig("nas.example", verify=value)


def test_connect_options_cannot_reach_the_check():
    with pytest.raises(ValueError, match="known_hosts"):
        SSHConfig("nas.example", connect_options={"known_hosts": None})


def _ssh_config(tmp_path, body: str):
    path = tmp_path / "ssh_config"
    path.write_text(body)
    return path


@pytest.mark.parametrize("leg", (_exec_connects, _sftp_connects))
def test_an_ssh_config_the_caller_names_is_honoured(server, tmp_path, leg):
    # The file is the caller's choice: its `UserKnownHostsFile none` stands.
    config_path = _ssh_config(tmp_path, "Host 127.0.0.1\n    UserKnownHostsFile none\n")
    assert leg(server.config(known_hosts=(), ssh_config=config_path))


def test_an_ssh_config_naming_the_file_still_verifies(server, tmp_path, empty_home):
    config_path = _ssh_config(
        tmp_path,
        f'Host 127.0.0.1\n    UserKnownHostsFile "{server.known_hosts_path}"\n',
    )
    assert _exec_connects(server.config(known_hosts=(), ssh_config=config_path))
    other = _ssh_config(tmp_path, "Host elsewhere\n    UserKnownHostsFile none\n")
    assert not _exec_connects(server.config(known_hosts=(), ssh_config=other))


def test_the_options_say_what_verify_chose(tmp_path):
    assert SSHConfig("nas.example").connect_opts()["known_hosts"] == ()
    assert SSHConfig("nas.example", verify=False).connect_opts()["known_hosts"] is None
    named = str(tmp_path / "known_hosts")
    for verify in (True, False):
        options = SSHConfig("nas.example", known_hosts=named, verify=verify)
        assert options.connect_opts()["known_hosts"] == named


def test_the_uri_key_round_trips():
    config = HostConfig("ssh://root@nas.example?verify=false")
    assert config.verify is False
    assert "verify=false" in config.connection_uri
    assert HostConfig(config.connection_uri).verify is False
    default = HostConfig("ssh://root@nas.example?verify=yes")
    assert default.verify is True
    # The default is left out, so existing URIs keep their text.
    assert "verify" not in default.connection_uri


def test_the_uri_key_takes_no_path():
    with pytest.raises(ValueError, match="verify"):
        HostConfig("ssh://root@nas.example?verify=%2Fetc%2Fssh%2Fknown_hosts")


def test_qga_over_ssh_inherits_the_switch():
    config = HostConfig("qga+ssh://root@pve.example/vm1?verify=false")
    assert isinstance(config, QEMUConfig)
    assert config.ssh.verify is False
    assert config.ssh.connect_opts()["known_hosts"] is None
    assert HostConfig(config.connection_uri).ssh.verify is False
    checked = HostConfig("qga+ssh://root@pve.example/vm1")
    assert checked.ssh.verify is True
    assert "verify" not in checked.connection_uri


@pytest.mark.parametrize(
    "uri",
    (
        "qemu+libvirt:///vm1?verify=false",
        "qga+unix:///run/vm1.qga?domain=vm1&verify=false",
        "qga+ssh://root@pve.example/vm1?verify=%2Fca.pem",
    ),
)
def test_qemu_refuses_verify_where_it_has_no_meaning(uri):
    with pytest.raises(ValueError, match="verify"):
        HostConfig(uri)


def test_qga_over_ssh_tunnel_refuses_an_unknown_host_key(server, empty_home):
    transport = server.config(known_hosts=())._create_transport()
    try:
        with pytest.raises(ConnectionError):
            transport.connect()
    finally:
        transport.close()
    assert not _authenticated(server)
