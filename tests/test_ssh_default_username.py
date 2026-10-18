"""`SSHConfig(default_username=)`: the account when nothing else names one.

Precedence, as the in-process asyncssh server sees it in `begin_auth`:
`username=` (or the URI's user) > the ssh_config's `User` > `default_username`
> the local account. `fake_home` (see `conftest.py`) keeps `~` in a temporary
directory, so `USER_SSH_CONFIG` reads a config written by the test.
"""

from __future__ import annotations

import dataclasses
import getpass
import os

import pytest

pytest.importorskip("asyncssh")

from hostctl import HostConfig, SSHConfig  # noqa: E402
from hostctl.transports.ssh import _config_source  # noqa: E402
from conformance.inprocess_ssh import InProcessSSHServer  # noqa: E402

ALIAS = "hostctl-alias.invalid"


async def _handler(process):
    process.exit(0)


@pytest.fixture
def server(tmp_path):
    with InProcessSSHServer(_handler) as running:
        yield running


def _alias(server, user=None) -> str:
    text = f"Host {ALIAS}\n    HostName 127.0.0.1\n    Port {server.port}\n"
    return text + (f"    User {user}\n" if user else "")


def _login(server, config: SSHConfig) -> str:
    """The account `config` logs in as."""
    server.events.clear()
    host = config._create_host()
    try:
        assert host.run("true").returncode == 0
    finally:
        host.close()
    [user] = [event[1] for event in server.events if event[0] == "begin_auth"]
    return user


def _config(server, **options) -> SSHConfig:
    options.setdefault("username", None)
    options.setdefault("connect_options", {"agent_path": None})
    return server.config(host=ALIAS, port=None, **options)


def test_the_default_applies_when_nothing_names_a_user(server, fake_home):
    (fake_home / ".ssh" / "config").write_text(_alias(server))

    assert _login(server, _config(server, default_username="admin")) == "admin"


def test_the_ssh_configs_user_wins_over_the_default(server, fake_home):
    (fake_home / ".ssh" / "config").write_text(_alias(server, user="operator"))

    assert _login(server, _config(server, default_username="admin")) == "operator"


def test_a_user_in_a_later_wildcard_block_still_wins(server, fake_home):
    """The ssh_config is read in full before the default: its `Host *`
    `User` counts, as it would for `ssh`."""
    text = _alias(server) + "Host *\n    User fallback\n"
    (fake_home / ".ssh" / "config").write_text(text)

    assert _login(server, _config(server, default_username="admin")) == "fallback"


def test_an_explicit_username_wins_over_both(server, fake_home):
    (fake_home / ".ssh" / "config").write_text(_alias(server, user="operator"))
    config = _config(server, username="named", default_username="admin")

    assert _login(server, config) == "named"


def test_the_uris_user_wins_over_both(server, fake_home):
    (fake_home / ".ssh" / "config").write_text(_alias(server, user="operator"))
    parsed = HostConfig(f"ssh://fromuri@{ALIAS}")
    config = dataclasses.replace(
        _config(server, default_username="admin"), username=parsed.username
    )

    assert _login(server, config) == "fromuri"


def test_without_a_default_the_local_account_logs_in(server, fake_home):
    (fake_home / ".ssh" / "config").write_text(_alias(server))

    assert _login(server, _config(server)) == getpass.getuser()


def test_the_default_follows_a_text_ssh_config(server, fake_home):
    with_user = _config(
        server, ssh_config=_alias(server, user="operator"), default_username="admin"
    )
    without = _config(server, ssh_config=_alias(server), default_username="admin")

    assert _login(server, with_user) == "operator"
    assert _login(server, without) == "admin"
    assert not _config_source._LIVE


def test_the_default_applies_with_no_ssh_config(server, fake_home):
    (fake_home / ".ssh" / "config").write_text(_alias(server, user="operator"))
    config = server.config(
        username=None,
        default_username="admin",
        ssh_config=None,
        connect_options={"agent_path": None},
    )

    assert _login(server, config) == "admin"


def test_every_configuration_file_exists_only_while_connecting(
    server, fake_home, monkeypatch
):
    (fake_home / ".ssh" / "config").write_text(_alias(server))
    removed = []
    remove = _config_source._remove

    def spy(name):
        with open(name, "rb") as stream:
            removed.append(stream.read())
        remove(name)
        assert not os.path.exists(name)

    monkeypatch.setattr(_config_source, "_remove", spy)
    config = _config(server, ssh_config=_alias(server), default_username="admin")

    assert _login(server, config) == "admin"
    assert removed == [_alias(server).encode(), b'User "admin"\n']
    assert not _config_source._LIVE


def test_the_default_is_in_repr_and_not_in_the_uri():
    config = SSHConfig("nas", default_username="root")

    assert "default_username='root'" in repr(config)
    assert "root" not in config.connection_uri
    assert SSHConfig("nas").default_username is None


def test_connect_options_carry_the_default_only_without_a_username(fake_home):
    assert SSHConfig("nas").connect_opts()["config"] is None
    assert (
        SSHConfig("nas", username="u", default_username="root").connect_opts()["config"]
        is None
    )
    [default] = SSHConfig("nas", default_username="root").connect_opts()["config"]
    assert default.text() == 'User "root"\n'


def test_a_backslash_survives_the_configuration_quoting():
    [default] = SSHConfig("nas", default_username="DOMAIN\\admin").connect_opts()[
        "config"
    ]
    assert default.text() == 'User "DOMAIN\\\\admin"\n'


@pytest.mark.parametrize(
    "value, message",
    [
        ("root\nProxyCommand touch /tmp/x", "cannot contain control characters"),
        ("-oProxyCommand=x", "cannot start with '-'"),
        ('ro"ot', "cannot contain"),
        ("none", "cannot be 'none'"),
        ("${HOME}", r"cannot contain '\$\{'"),
    ],
)
def test_a_default_that_would_change_the_configuration_is_refused(value, message):
    with pytest.raises(ValueError, match=f"default_username {message}"):
        SSHConfig("nas", default_username=value)
