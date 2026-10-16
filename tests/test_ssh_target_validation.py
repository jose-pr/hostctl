"""An SSH host is one name and a username is one account, as OpenSSH requires."""

from __future__ import annotations

import pytest

from hostctl.host._config import HostConfig
from hostctl.transports.ssh._config import SSHConfig

PASSWORD = "dummyPW"

REFUSED_URIS = (
    "ssh://nas -oProxyCommand=calc x/",
    "ssh://-oProxyCommand=id/",
    "ssh://a b;calc/",
    "ssh://x$(id)/",
    "ssh://${VERIFY_DUMMY}/",
    "qga+ssh://u@nas -oProxyCommand=calc x/vm",
    "qga+ssh://u@-oProxyCommand=id/vm",
)


@pytest.mark.parametrize("uri", REFUSED_URIS)
def test_a_uri_whose_host_is_not_one_name_is_refused(uri):
    with pytest.raises(ValueError) as raised:
        HostConfig(uri, password=PASSWORD)
    assert PASSWORD not in str(raised.value)


@pytest.mark.parametrize(
    "username", ("-oProxyCommand=x", "a -b", "x\\", "a;b", "${HOME}", "a`b")
)
def test_a_username_openssh_would_refuse_is_refused(username):
    with pytest.raises(ValueError) as raised:
        SSHConfig("h", username=username, password=PASSWORD)
    assert PASSWORD not in str(raised.value)


@pytest.mark.parametrize("host", ("-h", "a b", "a;b", "a$b", "a..b", "a\nb"))
def test_a_host_openssh_would_refuse_is_refused(host):
    with pytest.raises(ValueError):
        SSHConfig(host, password=PASSWORD)


@pytest.mark.parametrize(
    "host", ("::1", "fe80::1%eth0", "nas.example", "10.0.0.1", "nas", "host_1.lan")
)
def test_a_real_host_is_accepted(host):
    assert SSHConfig(host).host == host


@pytest.mark.parametrize(
    "username", ("DOMAIN\\user", "user@corp.example", "John Doe", "HOST$", "root")
)
def test_a_real_username_is_accepted(username):
    assert SSHConfig("h", username=username).username == username
