"""Every public config prints as a value and never prints a secret."""

from __future__ import annotations

import importlib
import inspect

import pytest

from hostctl import (
    ContainerConfig,
    HostConfig,
    IOSConfig,
    LocalConfig,
    LoginStep,
    PosixConfig,
    PromptConsoleProfile,
    QEMUConfig,
    SerialConfig,
    SSHConfig,
    SystemConfig,
    WindowsConfig,
    WinRMConfig,
)

SECRET = "S3cr3tValue"

_PUBLIC_MODULES = (
    "hostctl",
    "hostctl.host",
    "hostctl.system",
    "hostctl.transports.local",
    "hostctl.transports.ssh",
    "hostctl.transports.winrm",
    "hostctl.transports.container",
    "hostctl.transports.qemu",
    "hostctl.transports.serial",
)


def _ssh() -> SSHConfig:
    return SSHConfig(
        "nas.example",
        username="admin",
        password=SECRET,
        client_keys=[f"/keys/{SECRET}"],
        connect_options={"passphrase": SECRET},
    )


def _provider_options() -> dict:
    return {"ssh": _ssh(), "password": SECRET}


_FACTORIES = {
    LocalConfig: lambda: LocalConfig(),
    SystemConfig: lambda: SystemConfig("nas.example"),
    PosixConfig: lambda: PosixConfig(
        "nas.example",
        executor=("ssh",),
        path=("sftp",),
        provider_options=_provider_options(),
    ),
    WindowsConfig: lambda: WindowsConfig(
        "win.example", executor=("winrm",), provider_options=_provider_options()
    ),
    IOSConfig: lambda: IOSConfig(
        "switch.example", executor=("ssh",), provider_options=_provider_options()
    ),
    SSHConfig: _ssh,
    WinRMConfig: lambda: WinRMConfig("win.example", username="admin", password=SECRET),
    ContainerConfig: lambda: ContainerConfig("web", client_factory=lambda: SECRET),
    QEMUConfig: lambda: QEMUConfig("guest", transport="ssh", ssh=_ssh()),
    SerialConfig: lambda: SerialConfig(
        "/dev/ttyUSB0",
        profile=PromptConsoleProfile(
            "$ ", login=[LoginStep("Password: ", SECRET, secret=True)]
        ),
    ),
}


def _public_config_classes():
    found = set()
    for name in _PUBLIC_MODULES:
        module = importlib.import_module(name)
        for export in module.__all__:
            value = getattr(module, export)
            if (
                inspect.isclass(value)
                and issubclass(value, HostConfig)
                and value is not HostConfig
            ):
                found.add(value)
    return found


def test_every_public_config_class_has_a_factory_here():
    # A new public config must be added to `_FACTORIES`, or its repr goes
    # unchecked.
    assert _public_config_classes() == set(_FACTORIES)


@pytest.mark.parametrize("cls", sorted(_FACTORIES, key=lambda item: item.__name__))
def test_a_config_repr_names_its_class_and_hides_secrets(cls):
    shown = repr(_FACTORIES[cls]())
    assert "object at 0x" not in shown
    assert shown.startswith(f"{cls.__name__}(")
    assert SECRET not in shown


def test_a_password_in_a_uri_does_not_reach_the_repr():
    shown = repr(HostConfig(f"ssh://admin:{SECRET}@nas.example"))
    assert SECRET not in shown
    assert shown.startswith("SSHConfig(")


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (LocalConfig(), "LocalConfig()"),
        (
            PosixConfig("nas.example", executor=("ssh",), path=("sftp",)),
            "PosixConfig(authority='nas.example', shell=None, "
            "executor=('ssh',), path=('sftp',))",
        ),
        (LocalConfig(timeout=5), "LocalConfig(timeout=Timeout(total=5.0))"),
        (
            PosixConfig("nas.example", timeout=None),
            "PosixConfig(authority='nas.example', shell=None, "
            "executor=(), path=(), timeout=Timeout(total=None))",
        ),
    ],
)
def test_a_non_dataclass_config_repr_reads_like_its_constructor(config, expected):
    assert repr(config) == expected
