"""Explicit credentials override the ones a connection URI carries.

`HostConfig(uri, username=, password=)` and `Host(uri, ...)` let one URI be
used with different credentials without rewriting it.
"""

from __future__ import annotations

import pytest

from hostctl import Host, HostConfig
from hostctl.transports.winrm import WinRMConfig

SECRET = "S3cr3tValue"


def test_username_replaces_the_uri_user():
    config = HostConfig("ssh://alice:pw@nas.example", username="bob")

    assert config.username == "bob"
    assert config.password == "pw"
    assert config.connection_uri.startswith("ssh://bob@nas.example")


def test_username_adds_a_user_to_a_uri_without_one():
    assert HostConfig("ssh://nas.example", username="bob").username == "bob"


def test_password_replaces_the_uri_password():
    config = HostConfig("ssh://alice:old@nas.example", password=SECRET)

    assert config.password == SECRET
    assert config.username == "alice"
    assert SECRET not in repr(config) and SECRET not in config.connection_uri


def test_password_replaces_the_whole_password_field_extras_included():
    # Without the override the URI's `otp` extra reaches the config and SSH
    # refuses it; the explicit password replaces the field, extra and all.
    with pytest.raises(ValueError, match="otp"):
        HostConfig("ssh://alice:old%0Aotp:123@nas.example")
    config = HostConfig("ssh://alice:old%0Aotp:123@nas.example", password="new")
    assert config.password == "new"


def test_an_empty_password_means_none():
    assert HostConfig("ssh://alice:old@nas.example", password="").password is None
    assert HostConfig("ssh://nas.example", password="").password is None
    # A scheme that takes no password is not offered one.
    assert str(HostConfig("local:", password="")) == "local:"


def test_a_password_of_none_keeps_the_uris():
    assert HostConfig("ssh://alice:pw@nas.example", password=None).password == "pw"


def test_the_overrides_reach_a_host_built_from_a_uri():
    host = Host("ssh://alice:old@nas.example", username="bob", password=SECRET)

    assert host.config.username == "bob"
    assert host.config.password == SECRET


def test_winrm_takes_both_and_encodes_a_domain_user():
    config = HostConfig("winrm://alice:pw@win.example", username="CORP\\bob")

    assert isinstance(config, WinRMConfig)
    assert config.username == "CORP\\bob"
    assert config.password == "pw"
    assert "CORP%5Cbob@win.example" in config.connection_uri
    assert HostConfig(config.connection_uri).username == "CORP\\bob"


def test_qga_over_ssh_takes_the_tunnel_user():
    config = HostConfig("qga+ssh://alice@hv.example/vm", username="bob", password="pw")

    assert config.ssh.username == "bob"
    assert config.ssh.password == "pw"


@pytest.mark.parametrize(
    "uri", ("local:", "docker://web", "qemu+libvirt:///vm", "serial:///dev/ttyS0")
)
def test_a_scheme_without_a_login_user_refuses_username(uri):
    with pytest.raises(ValueError, match="username"):
        HostConfig(uri, username="bob")


def test_supported_credentials_name_username_where_it_applies():
    assert "username" in HostConfig.supported_credentials("ssh://h")
    assert "username" in HostConfig.supported_credentials("winrm://u@h")
    assert "username" in HostConfig.supported_credentials("qga+ssh://u@h/vm")
    assert "username" not in HostConfig.supported_credentials("docker://web")
