"""Every optional extra names itself when its dependency is missing.

Each row hides the extra's import, makes the first public call that reaches
its guard, and asserts the message says which extra to install. No row
reaches the network: the import is the first thing the call does.
"""

from __future__ import annotations

import importlib.abc
import sys

import pytest

from hostctl.exceptions import HostctlError, MissingExtraError


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
    from hostctl.transports.ssh._config import SSHConfig

    SSHConfig("nas.example", verify=False).open().run("true")


def _ssh_path():
    from hostctl.transports.ssh._config import SSHConfig
    from hostctl.transports.ssh._providers import ssh_providers

    ssh_providers(SSHConfig("nas.example", verify=False))[1].path("/")


def _winrm_run():
    from hostctl.transports.winrm._config import WinRMConfig

    WinRMConfig(
        "win.example", username="u", password="p", provider="pywinrm"
    ).open().run("echo hi")


def _psrp():
    from hostctl.transports.winrm._psrp import RunspaceSession

    RunspaceSession(pool=None)._make_pool()


def _container_run():
    from hostctl.transports.container._config import ContainerConfig

    ContainerConfig("web").open().run("true")


def _serial_open():
    from hostctl.transports.serial._executor import _default_serial_factory

    _default_serial_factory("loop://")


def _libvirt():
    from hostctl.transports.qemu._qga_transports import LibvirtGuestAgentTransport

    LibvirtGuestAgentTransport("vm", connection_uri="qemu:///system").connect()


def _deploy():
    import tempfile

    from hostctl.deploy import bundle

    with tempfile.TemporaryDirectory() as scratch:
        bundle("hostctl", scratch + "/x.pyz")


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
    # pathspec, not packaging: pytest itself imports packaging while it runs.
    pytest.param("deploy", ("pathspec",), _deploy, id="deploy"),
)


@pytest.mark.parametrize("extra, modules, call", ROWS)
def test_a_missing_dependency_names_the_extra_to_install(hide, extra, modules, call):
    hide(*modules)

    with pytest.raises(MissingExtraError) as raised:
        call()

    assert f'pip install "hostctl[{extra}]"' in str(raised.value)
    assert raised.value.extra == extra
    assert isinstance(raised.value, ImportError)
    # The import that failed stays reachable for a traceback.
    assert isinstance(raised.value.__cause__, ImportError)


def test_an_older_python_than_the_extra_supports_names_the_python(monkeypatch):
    from hostctl import _extras

    monkeypatch.setattr(_extras.sys, "version_info", (3, 9, 0))
    with pytest.raises(MissingExtraError) as raised:
        _extras._require("psrp", "PSRP")

    assert raised.value.python == (3, 10)
    assert "Python 3.10 or newer" in str(raised.value)
    assert "pip install" not in str(raised.value)


def test_missing_extra_error_is_a_hostctl_error_and_an_import_error():
    error = MissingExtraError("ssh", "SSH")

    assert isinstance(error, HostctlError) and isinstance(error, ImportError)
    assert (error.extra, error.feature, error.python) == ("ssh", "SSH", None)
    assert str(error) == "SSH needs the 'ssh' extra: pip install \"hostctl[ssh]\""


def test_a_probe_reports_an_incomplete_ssh_extra(hide):
    from hostctl.transports.ssh._providers import SFTPPathProvider, ssh_providers
    from hostctl.transports.ssh._config import SSHConfig

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
