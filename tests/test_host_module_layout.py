"""Host implementations remain private modules with top-level exports."""

import importlib.util

import hostctl


def test_transport_implementations_are_not_public_module_shims():
    for name in (
        "hostctl.host.local",
        "hostctl.host.ssh",
        "hostctl.host.winrm",
        "hostctl.host.winrm_path",
        "hostctl.host._winrm_path",
        "hostctl.host.qemu_path",
    ):
        assert importlib.util.find_spec(name) is None


def test_transport_classes_remain_available_from_supported_surface():
    assert hostctl.LocalHost
    assert hostctl.SshHost
    assert hostctl.WinRMHost
    assert hostctl.WinRMPath
