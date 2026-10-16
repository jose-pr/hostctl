"""One home per transport: the 0.3 module paths are gone, and so are the
re-exported backends."""

import importlib.util

import pytest

import hostctl

#: Module paths 0.3 had; each name moved to a public home or went internal.
_GONE = (
    "hostctl.host._common",
    "hostctl.host._connection",
    "hostctl.host._local",
    "hostctl.host._ssh",
    "hostctl.host._winrm",
    "hostctl.host._qga_helper",
    "hostctl.host._staged_io",
    "hostctl.host._backend_path",
    "hostctl.host.composite_path",
    "hostctl.host.container",
    "hostctl.host.container_path",
    "hostctl.host.qemu",
    "hostctl.host.serial",
    "hostctl.host.system",
    "hostctl.host.local",
    "hostctl.host.ssh",
    "hostctl.host.winrm",
    "hostctl.executor._common",
    "hostctl.executor._qga",
    "hostctl.executor.container",
    "hostctl.executor.local",
    "hostctl.executor.psrp",
    "hostctl.executor.qemu",
    "hostctl.executor.serial",
    "hostctl.executor.ssh",
    "hostctl.executor.winrm",
    "hostctl.process._common",
    "hostctl.process.container",
    "hostctl.process.psrp",
    "hostctl.process.qemu_serial",
    "hostctl.process.serial",
    "hostctl.process.ssh",
    "hostctl.provider._common",
    "hostctl.provider.transports",
    "hostctl.serial",
    "hostctl.shell._common",
    "hostctl.shell.cmd",
    "hostctl.shell.fish",
    "hostctl.shell.posix",
    "hostctl.shell.powershell",
)


def _absent(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is None
    except ModuleNotFoundError:
        # The parent is a plain module now (`hostctl.executor`), not a package.
        return True


@pytest.mark.parametrize("name", _GONE)
def test_a_03_module_path_no_longer_imports(name):
    assert _absent(name)


def test_backends_and_the_path_alias_are_not_exported():
    assert "WinRMPath" not in hostctl.__all__
    assert "HostPath" not in hostctl.__all__
    assert not hasattr(hostctl, "WinRMPath")
    assert not hasattr(hostctl, "HostPath")


def test_transport_classes_remain_available_from_supported_surface():
    assert hostctl.LocalHost
    assert hostctl.PosixHost
    assert hostctl.WindowsHost
    assert not hasattr(hostctl, "SshHost")
    assert not hasattr(hostctl, "WinRMHost")
