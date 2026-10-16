"""Every optional extra names itself when its dependency is missing.

Each row hides the extra's import, makes the first public call that reaches
its guard, and asserts the message says which extra to install. No row
reaches the network: the import is the first thing the call does.
"""

from __future__ import annotations

import importlib.abc
import sys

import pytest


class _Hidden(importlib.abc.MetaPathFinder):
    def __init__(self, names):
        self.names = tuple(names)

    def find_spec(self, fullname, path, target=None):
        if fullname.split(".")[0] in self.names:
            raise ImportError(f"{fullname} hidden by the test")
        return None


@pytest.fixture
def hide(monkeypatch):
    def apply(*names):
        for module in list(sys.modules):
            if module.split(".")[0] in names:
                monkeypatch.delitem(sys.modules, module)
        finder = _Hidden(names)
        sys.meta_path.insert(0, finder)
        return finder

    yield apply
    sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, _Hidden)]


def _ssh_run():
    from hostctl import SSHConfig

    SSHConfig("nas.example", known_hosts=None).open().run("true")


def _ssh_path():
    from hostctl import SSHConfig
    from hostctl.host._ssh import ssh_providers

    ssh_providers(SSHConfig("nas.example", known_hosts=None))[1].path("/")


def _winrm_run():
    from hostctl import WinRMConfig

    WinRMConfig(
        "win.example", username="u", password="p", provider="pywinrm"
    ).open().run("echo hi")


def _psrp():
    from hostctl.executor.psrp import require_pypsrp

    require_pypsrp()


def _container_run():
    from hostctl import ContainerConfig

    ContainerConfig("web").open().run("true")


def _serial_open():
    from hostctl.executor.serial import _default_serial_factory

    _default_serial_factory("loop://")


def _libvirt():
    from hostctl.executor._qga import LibvirtGuestAgentTransport

    LibvirtGuestAgentTransport("vm", connection_uri="qemu:///system").connect()


ROWS = (
    pytest.param("ssh", ("asyncssh",), _ssh_run, id="ssh-asyncssh"),
    pytest.param("ssh", ("sftppath",), _ssh_path, id="ssh-sftppath"),
    pytest.param("winrm", ("winrm",), _winrm_run, id="winrm"),
    pytest.param(
        "psrp",
        ("pypsrp",),
        _psrp,
        id="psrp",
        marks=pytest.mark.skipif(
            sys.version_info < (3, 10), reason="PSRP needs Python 3.10+"
        ),
    ),
    pytest.param("container", ("docker",), _container_run, id="container"),
    pytest.param("serial", ("serial",), _serial_open, id="serial"),
    pytest.param("qemu-libvirt", ("libvirt", "libvirt_qemu"), _libvirt, id="libvirt"),
)


@pytest.mark.parametrize("extra, modules, call", ROWS)
def test_a_missing_dependency_names_the_extra_to_install(hide, extra, modules, call):
    hide(*modules)

    with pytest.raises(ImportError) as raised:
        call()

    assert f"hostctl[{extra}]" in str(raised.value)


def test_a_probe_reports_an_incomplete_ssh_extra(hide):
    from hostctl.host._ssh import SFTPPathProvider, ssh_providers
    from hostctl import SSHConfig

    hide("sftppath")
    _, paths = ssh_providers(SSHConfig("nas.example"))

    probe = paths.probe()

    assert probe.availability == "unavailable"
    assert "sftppath" in probe.reason and "hostctl[ssh]" in probe.reason
    assert isinstance(paths, SFTPPathProvider)


def test_without_duho_the_cli_names_the_extra(hide):
    from hostctl.cli import main

    hide("duho")
    with pytest.raises(SystemExit) as exit_:
        main(["info", "local:"])

    assert "hostctl[cli]" in str(exit_.value.code)
