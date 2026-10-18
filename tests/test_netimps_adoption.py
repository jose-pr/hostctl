"""Default ports, host bracketing and local-host checks come from netimps."""

import os
import pathlib
import subprocess
import sys

import pytest

import hostctl
from hostctl.uri._connection import ConnectionString
from hostctl.host._config import HostConfig
from hostctl.system._kinds import PosixConfig
from hostctl.transports.ssh._config import SSHConfig
from hostctl.transports.winrm._config import WinRMConfig
from hostctl.transports.qemu._config import QEMUConfig


def test_importing_hostctl_does_not_import_netimps():
    """netimps patches `socket` on Windows when imported, so hostctl imports
    it where it is used and `import hostctl` stays free of that effect."""
    source = str(pathlib.Path(hostctl.__file__).resolve().parents[1])
    code = "import sys, hostctl; print('netimps' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": source},
    )

    assert result.stdout.strip() == "False"


def test_a_winrm_endpoint_defaults_to_the_scheme_port():
    assert (
        WinRMConfig("h", username="u", password="x").endpoint == "http://h:5985/wsman"
    )
    assert (
        WinRMConfig("h", username="u", password="x", tls=True).endpoint
        == "https://h:5986/wsman"
    )
    assert (
        WinRMConfig("h", username="u", password="x", port=1234).endpoint
        == "http://h:1234/wsman"
    )


def test_an_ipv6_host_is_bracketed_beside_its_port():
    assert (
        WinRMConfig("::1", username="u", password="x", port=5985).endpoint
        == "http://[::1]:5985/wsman"
    )
    assert SSHConfig("::1", port=2222).connection_uri.startswith("ssh://[::1]:2222?")


def test_an_ssh_uri_without_a_port_leaves_it_to_the_ssh_config():
    # asyncssh then takes the ssh_config's `Port`, else 22, as `ssh` does.
    assert HostConfig("ssh://u@nas").port is None
    assert HostConfig("ssh://u@nas:22").port == 22
    config = HostConfig("ssh://u@[::1]")
    assert (config.host, config.port) == ("::1", None)
    assert config.connection_uri.startswith("ssh://u@[::1]?")


def test_the_qga_ssh_uri_leaves_the_port_to_the_ssh_config_too():
    config = HostConfig("qga+ssh://u@nas/vm")

    assert isinstance(config, QEMUConfig)
    assert config.ssh.port is None
    assert config.connection_uri.startswith("qga+ssh://u@nas/vm?")


@pytest.mark.parametrize(
    "authority, hostname",
    [
        ("root@node:2222", "node"),
        ("node", "node"),
        ("[::1]:22", "::1"),
        ("node:notaport", "node:notaport"),
    ],
)
def test_the_reported_hostname_is_the_host_without_userinfo_or_port(
    authority, hostname
):
    host = PosixConfig(authority, executor=("local",))._create_host()

    assert host.info().hostname == hostname


@pytest.mark.parametrize(
    "host, local",
    [
        ("localhost", True),
        ("LOCALHOST", True),
        ("app.localhost", True),
        ("127.0.0.1", True),
        ("127.1", True),
        ("[::1]", True),
        ("nas.example.invalid", False),
    ],
)
def test_is_local_is_netimps_is_local_host(host, local):
    assert ConnectionString(host, scheme="wss").is_local is local


def test_an_empty_host_is_local():
    assert ConnectionString("local:").is_local
