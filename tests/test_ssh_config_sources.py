"""Where an SSH host's ssh_config comes from: the user's, a file, or text.

Every test runs against the in-process asyncssh server, and `fake_home` (see
`conftest.py`) keeps `~` in a temporary directory. An alias that only the
config resolves to 127.0.0.1 proves the config was read; the same alias
failing to resolve proves it was not.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import os
import pathlib
import socket
import sys

import pytest

pytest.importorskip("asyncssh")

from pathapi import MemPath  # noqa: E402

from hostctl import USER_SSH_CONFIG, HostConfig, SSHConfig  # noqa: E402
from hostctl.transports.ssh import _config_source  # noqa: E402
from conformance.inprocess_ssh import InProcessSSHServer  # noqa: E402

ALIAS = "hostctl-alias.invalid"
MARKER = "hostctl-config-text-marker"


async def _handler(process):
    process.exit(0)


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "sftp"
    root.mkdir()
    with InProcessSSHServer(_handler, sftp_root=root) as running:
        yield running


def _alias(server) -> str:
    return (
        f"# {MARKER}\n"
        f"Host {ALIAS}\n"
        "    HostName 127.0.0.1\n"
        f"    Port {server.port}\n"
    )


def _config(server, **options) -> SSHConfig:
    options.setdefault("connect_options", {"agent_path": None})
    return server.config(host=ALIAS, **options)


def _both_legs(config: SSHConfig) -> None:
    host = config._create_host()
    try:
        assert host.run("true").returncode == 0
        assert host.path("/").exists()
    finally:
        host.close()


def _causes(exc):
    while exc is not None:
        yield exc
        exc = exc.__cause__ or exc.__context__


def _reaches_alias(server, config: SSHConfig) -> bool:
    try:
        _both_legs(config)
    except Exception as exc:  # noqa: BLE001 - the cause is checked below
        assert any(isinstance(c, socket.gaierror) for c in _causes(exc)), exc
        assert not [e for e in server.events if e[0] == "begin_auth"]
        return False
    begun = [e for e in server.events if e[0] == "begin_auth"]
    assert len(begun) == 1  # run() and path() share one connection
    return True


# -- the default: the user's own ~/.ssh/config --------------------------------


def test_the_default_is_the_users_ssh_config(server, fake_home):
    (fake_home / ".ssh" / "config").write_text(_alias(server))
    config = _config(server)

    assert config.ssh_config is USER_SSH_CONFIG
    assert _reaches_alias(server, config)


def test_the_users_config_is_found_when_connecting(server, fake_home, monkeypatch):
    config = _config(server)
    later = fake_home.parent / "later"
    (later / ".ssh").mkdir(parents=True)
    (later / ".ssh" / "config").write_text(_alias(server))
    monkeypatch.setenv("HOME", str(later))
    monkeypatch.setenv("USERPROFILE", str(later))

    assert _reaches_alias(server, config)


def test_a_missing_users_config_reads_nothing_and_fails_nothing(server, fake_home):
    assert SSHConfig("h").connect_opts()["config"] is None
    host = server.config(connect_options={"agent_path": None})._create_host()
    try:
        assert host.run("true").returncode == 0
    finally:
        host.close()
    server.events.clear()
    assert not _reaches_alias(server, _config(server))


@pytest.mark.parametrize("value", [None, False])
def test_none_and_false_read_no_config(server, fake_home, value):
    (fake_home / ".ssh" / "config").write_text(_alias(server))
    config = _config(server, ssh_config=value)

    assert config.ssh_config is None
    assert not _reaches_alias(server, config)


def test_without_a_port_the_configs_port_applies(server, fake_home):
    (fake_home / ".ssh" / "config").write_text(_alias(server))
    config = _config(server, port=None)

    assert config.connect_opts().get("port") is None
    assert _reaches_alias(server, config)
    assert f"{ALIAS}?" in config.connection_uri


def test_a_named_port_wins_over_the_configs(server, fake_home):
    (fake_home / ".ssh" / "config").write_text("Host 127.0.0.1\n    Port 1\n")
    host = server.config(connect_options={"agent_path": None})._create_host()
    try:
        assert host.run("true").returncode == 0
    finally:
        host.close()


def test_known_hosts_default_follows_the_users_config(server, fake_home):
    known = pathlib.Path(server.known_hosts_path).as_posix()
    ssh = fake_home / ".ssh"
    (ssh / "config").write_text(f'Host 127.0.0.1\n    UserKnownHostsFile "{known}"\n')
    options = {"agent_path": None}

    _both_legs(server.config(known_hosts=(), connect_options=options))
    (ssh / "config").write_text("")
    with pytest.raises(Exception):
        _both_legs(server.config(known_hosts=(), connect_options=options))
    # `verify=False` checks nothing, whatever the config names.
    (ssh / "empty_known_hosts").write_text("")
    empty = (ssh / "empty_known_hosts").as_posix()
    (ssh / "config").write_text(f'Host 127.0.0.1\n    UserKnownHostsFile "{empty}"\n')
    _both_legs(server.config(known_hosts=(), verify=False, connect_options=options))


# -- a file ---------------------------------------------------------------------


def test_an_os_pathlike_names_a_file(server, fake_home, tmp_path):
    path = tmp_path / "ssh_config"
    path.write_text(_alias(server))
    config = _config(server, ssh_config=path)

    assert config.ssh_config == path
    assert config.connect_opts()["config"] == [str(path)]
    assert _reaches_alias(server, config)


def test_a_pathlike_is_expanded_when_connecting(server, fake_home):
    (fake_home / "named_config").write_text(_alias(server))

    assert _reaches_alias(
        server, _config(server, ssh_config=pathlib.Path("~/named_config"))
    )


# -- text -----------------------------------------------------------------------


_TEXT_SOURCES = {
    "str": lambda text: text,
    "bytes": lambda text: text.encode(),
    "bytearray": lambda text: bytearray(text.encode()),
    "memoryview": lambda text: memoryview(text.encode()),
    "StringIO": lambda text: io.StringIO(text),
    "BytesIO": lambda text: io.BytesIO(text.encode()),
}


@pytest.mark.parametrize("kind", sorted(_TEXT_SOURCES))
def test_text_sources_are_the_configuration(server, fake_home, kind):
    config = _config(server, ssh_config=_TEXT_SOURCES[kind](_alias(server)))

    assert repr(config.ssh_config) == "<ssh_config text>"
    assert _reaches_alias(server, config)


def test_a_pathapi_path_is_read_through_the_object(server, fake_home):
    source = MemPath("/etc/ssh/ssh_config")
    source.parent.mkdir(parents=True)
    source.write_text(_alias(server))

    assert _reaches_alias(server, _config(server, ssh_config=source))


def test_a_local_pathapi_path_is_a_file(server, fake_home, tmp_path):
    from pathapi import Path

    (tmp_path / "ssh_config").write_text(_alias(server))
    source = Path(str(tmp_path / "ssh_config"))
    config = _config(server, ssh_config=source)

    assert config.connect_opts()["config"] == [os.fspath(source)]
    assert _reaches_alias(server, config)


def test_a_stream_is_read_once_when_the_config_is_built(server, fake_home):
    stream = io.StringIO(_alias(server))
    config = _config(server, ssh_config=stream)
    stream.seek(0)
    stream.truncate()
    stream.write("Host *\n    Port 1\n")

    assert _reaches_alias(server, dataclasses.replace(config))


class _Counting:
    """An object with `read_text`, counting the reads."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.reads = 0

    def read_text(self, encoding=None):
        self.reads += 1
        return self.text


def test_a_path_object_is_read_at_the_first_connect_and_kept(server, fake_home):
    source = _Counting(_alias(server))
    config = _config(server, ssh_config=source)
    copy = dataclasses.replace(config, verify=True)
    assert source.reads == 0  # constructing reads nothing

    assert _reaches_alias(server, config)
    source.text = "Host *\n    Port 1\n"
    server.events.clear()
    assert _reaches_alias(server, copy)
    assert source.reads == 1


def test_a_source_read_through_its_own_connection_raises():
    class _Loop:
        def read_text(self, encoding=None):
            return config.ssh_config.text()

    config = SSHConfig("h", ssh_config=_Loop())
    with pytest.raises(RuntimeError, match="connection it configures"):
        config.ssh_config.text()


def test_the_text_file_exists_only_while_connecting(server, fake_home, monkeypatch):
    seen = []
    remove = _config_source._remove

    def spy(name):
        with open(name, "rb") as stream:
            seen.append((name, stream.read(), os.stat(name).st_mode & 0o777))
        remove(name)

    monkeypatch.setattr(_config_source, "_remove", spy)
    text = _alias(server)
    host = _config(server, ssh_config=text)._create_host()
    try:
        assert host.run("true").returncode == 0
        [(name, data, mode)] = seen
        assert not os.path.exists(name)  # gone while the connection is open
        assert data == text.encode() and b"\r\n" not in data
        if sys.platform != "win32":
            assert mode == 0o600
    finally:
        host.close()
    assert not _config_source._LIVE


def test_the_exit_handler_removes_a_leftover(tmp_path):
    leftover = tmp_path / "hostctl-ssh-config-x.conf"
    leftover.write_text("Port 1\n")
    _config_source._LIVE.add(str(leftover))

    _config_source._remove_leftovers()

    assert not leftover.exists() and not _config_source._LIVE


def test_the_text_never_reaches_repr_uri_or_log(server, fake_home, caplog):
    caplog.set_level(logging.DEBUG)
    config = _config(server, ssh_config=_alias(server))

    assert "ssh_config=<ssh_config text>" in repr(config)
    _both_legs(config)
    for shown in (repr(config), str(config), config.connection_uri, caplog.text):
        assert MARKER not in shown


def test_repr_names_the_source_kind():
    assert "ssh_config=USER_SSH_CONFIG" in repr(SSHConfig("h"))
    assert "ssh_config=None" in repr(SSHConfig("h", ssh_config=False))
    path = pathlib.Path("cfg")
    assert f"ssh_config={path!r}" in repr(SSHConfig("h", ssh_config=path))


# -- what is refused --------------------------------------------------------------


@pytest.mark.parametrize("value", ["~/.ssh/config", "C:\\Users\\me\\.ssh\\config"])
def test_a_str_holding_a_file_name_is_refused(value):
    with pytest.raises(ValueError, match=r"pathlib\.Path") as caught:
        SSHConfig("h", ssh_config=value)
    assert value not in str(caught.value)


def test_bytes_that_are_not_utf8_are_refused():
    with pytest.raises(ValueError, match="UTF-8"):
        SSHConfig("h", ssh_config=b"Host \xff\n")


@pytest.mark.parametrize("value", [True, 3, object()])
def test_other_values_are_refused(value):
    with pytest.raises((TypeError, ValueError), match="ssh_config"):
        SSHConfig("h", ssh_config=value)


def test_a_uri_cannot_name_an_ssh_config():
    with pytest.raises(ValueError, match="ssh_config"):
        HostConfig("ssh://h?ssh_config=/tmp/x")
    with pytest.raises((TypeError, ValueError), match="ssh_config"):
        HostConfig("ssh://h", ssh_config="Host h\n")


def test_the_marker_survives_pickle_and_copy():
    import copy
    import pickle

    assert pickle.loads(pickle.dumps(USER_SSH_CONFIG)) is USER_SSH_CONFIG
    assert copy.deepcopy(USER_SSH_CONFIG) is USER_SSH_CONFIG
    assert repr(USER_SSH_CONFIG) == "USER_SSH_CONFIG"
