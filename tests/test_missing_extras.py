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


def test_a_library_built_on_hostctl_names_its_own_distribution():
    error = MissingExtraError("ssh", "SFTP paths", distribution="pytruenas")

    assert error.distribution == "pytruenas"
    assert str(error) == (
        "SFTP paths needs the 'ssh' extra: pip install \"pytruenas[ssh]\""
    )
    assert MissingExtraError("ssh", "SSH").distribution == "hostctl"


def test_the_python_floor_message_names_the_distribution_and_no_pip_hint():
    error = MissingExtraError("psrp", "PSRP", python=(3, 10), distribution="mytool")

    assert str(error) == (
        "PSRP needs Python 3.10 or newer "
        "(the 'psrp' extra of mytool supports nothing older)"
    )
    assert "pip install" not in str(error)


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

    assert str(exit_.value.code) == (
        "hostctl: the hostctl command line needs the 'cli' extra: "
        'pip install "hostctl[cli]"'
    )


#: What reaches each extra from the command line; none of them gets as far as
#: the network, because the import is the first thing the connection does.
CLI_ROWS = (
    pytest.param(
        "ssh", ("asyncssh",), ["run", "ssh://nas.example", "--", "true"], id="ssh"
    ),
    pytest.param(
        "winrm",
        ("winrm",),
        ["run", "winrm://u@win.example:5985?provider=pywinrm", "--", "x"],
        id="winrm",
    ),
    pytest.param(
        "psrp",
        ("pypsrp",),
        ["run", "winrm://u@win.example:5985?provider=psrp", "--", "x"],
        id="psrp",
        marks=pytest.mark.skipif(
            sys.version_info < (3, 10), reason="PSRP needs Python 3.10+"
        ),
    ),
    pytest.param(
        "container", ("docker",), ["run", "docker://web", "--", "true"], id="container"
    ),
    pytest.param(
        "serial", ("serial",), ["run", "serial:///dev/ttyS0", "--", "true"], id="serial"
    ),
    pytest.param(
        "qemu-libvirt",
        ("libvirt", "libvirt_qemu"),
        ["info", "qemu+libvirt:///vm"],
        id="libvirt",
    ),
    pytest.param(
        "deploy",
        ("pathspec",),
        ["deploy", "--tree", "local:", ".", "x.pyz"],
        id="deploy",
    ),
)


@pytest.fixture
def cli(monkeypatch):
    pytest.importorskip("duho")
    monkeypatch.delenv("DUHO_TRACEBACK", raising=False)
    monkeypatch.setenv("HOSTCTL_PASSWORD", "pw")
    from hostctl.cli import main

    return main


@pytest.mark.parametrize("extra, modules, argv", CLI_ROWS)
def test_the_cli_reports_a_missing_extra_in_one_line(
    cli, hide, capsys, extra, modules, argv
):
    hide(*modules)

    status = cli(argv)

    err = capsys.readouterr().err
    assert status == 125
    assert len(err.splitlines()) == 1 and err.startswith("hostctl: "), err
    assert f'pip install "hostctl[{extra}]"' in err
    assert "Traceback" not in err


@pytest.mark.skipif(sys.version_info >= (3, 10), reason="the floor applies before 3.10")
def test_the_cli_names_the_python_psrp_needs(cli, capsys):
    status = cli(["run", "winrm://u@win.example:5985?provider=psrp", "--", "x"])

    err = capsys.readouterr().err
    assert status == 125
    assert "Python 3.10 or newer" in err and len(err.splitlines()) == 1


def test_an_import_error_inside_a_dependency_keeps_its_traceback(cli, monkeypatch):
    import hostctl.cli.info as info

    def broken(host):
        raise ImportError("cannot import name 'thing' from 'somedependency'")

    monkeypatch.setattr(info, "_identity", broken)

    with pytest.raises(ImportError, match="somedependency") as raised:
        cli(["info", "local:"])
    assert not isinstance(raised.value, MissingExtraError)


def test_the_cli_reports_a_downstream_distribution_s_extra_as_its_own(
    cli, capsys, monkeypatch
):
    """The 125 mapping is the type's, so a provider from another package that
    raises it for its own extra gets the same one line."""
    import hostctl.cli.info as info

    def missing(host):
        raise MissingExtraError("ssh", "the web shell", distribution="pytruenas")

    monkeypatch.setattr(info, "_identity", missing)

    status = cli(["info", "local:"])

    err = capsys.readouterr().err
    assert status == 125
    assert err.strip() == (
        "hostctl: the web shell needs the 'ssh' extra: " 'pip install "pytruenas[ssh]"'
    )
