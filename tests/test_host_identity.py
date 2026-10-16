"""A host names itself in logs, and a provider fallback is audible.

`Host.name` is a short, credential-free label; `Host.logger` prefixes it to
every record and sets `record.host`. When `run()` or a path operation is
served by a provider ranked below one that is unavailable, the host logs ONE
WARNING per skipped provider -- not one per call, and none for routing an
operation to the provider that can do it, or for a caller's pin.
"""

from __future__ import annotations

import dataclasses
import logging
import subprocess

import pytest
from pathapi import Path

from hostctl import (
    ContainerConfig,
    ExecutorProvider,
    HostConfig,
    LocalHost,
    OperationNotStartedError,
    PathProvider,
    PosixConfig,
    PosixHost,
    ProviderDetail,
    ProviderProbe,
    QEMUConfig,
    SelectionStep,
    SerialConfig,
    SSHConfig,
    WinRMConfig,
)


def _execute(command, *args, **options):
    return subprocess.CompletedProcess((command, *args), 0, b"ok", b"")


class _Refusing(ExecutorProvider):
    """Declines before dispatch, as an unreachable SSH server does."""

    def __init__(self, name):
        super().__init__(name, _execute)

    def connect(self):
        raise OperationNotStartedError(f"{self.name} refused: connection lost")


def _fallback_warnings(caplog):
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "fell back" in record.getMessage()
    ]


# --- typed records -----------------------------------------------------------


def test_selection_steps_and_provider_details_are_frozen_records():
    host = PosixHost(
        executor_providers=(_Refusing("ssh"), ExecutorProvider("web", _execute))
    )
    host.run("true")

    first, second = host.last_selection
    assert isinstance(first, SelectionStep)
    assert (first.provider, first.availability, first.chosen) == (
        "ssh",
        "unavailable",
        False,
    )
    assert "connection lost" in first.reason
    assert (second.provider, second.chosen) == ("web", True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.chosen = True

    details = host.provider_details
    assert all(isinstance(item, ProviderDetail) for item in details)
    assert [(item.kind, item.provider, item.availability) for item in details] == [
        ("executor", "ssh", "unavailable"),
        ("executor", "web", "available"),
    ]


# --- name ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "config,expected",
    [
        (SSHConfig("nas.example", username="root", password="hunter2"), "nas.example"),
        (SSHConfig("nas.example", port=2222), "nas.example:2222"),
        (SSHConfig("2001:db8::1"), "[2001:db8::1]"),
        (WinRMConfig("win.example", username="u", password="hunter2"), "win.example"),
        (PosixConfig("root@node:2222"), "node:2222"),
        (ContainerConfig("web"), "web"),
        (QEMUConfig("vm1"), "vm1"),
        (SerialConfig("/dev/ttyUSB0"), "/dev/ttyUSB0"),
        (HostConfig("local:"), "localhost"),
    ],
)
def test_a_config_names_its_target_without_credentials(config, expected):
    assert config.name == expected
    assert "hunter2" not in config.name and "root" not in config.name


def test_a_host_takes_its_name_from_the_config_and_may_be_renamed():
    host = SSHConfig("nas.example", port=2222).open()
    assert host.name == "nas.example:2222"

    host.name = "nas1"
    assert host.name == "nas1"
    assert LocalHost().name == "localhost"


# --- logger --------------------------------------------------------------------


def test_the_host_logger_prefixes_the_name_and_sets_record_host(caplog):
    first = PosixHost(PosixConfig("nas1"))
    second = PosixHost(PosixConfig("nas2"))

    with caplog.at_level(logging.INFO, logger="hostctl"):
        first.logger.info("connection was closed")
        second.logger.info("connection was closed")
        first.logger.info("[nas1] already prefixed")

    assert [record.getMessage() for record in caplog.records] == [
        "[nas1] connection was closed",
        "[nas2] connection was closed",
        "[nas1] already prefixed",
    ]
    assert [record.host for record in caplog.records] == ["nas1", "nas2", "nas1"]
    assert all(record.name.startswith("hostctl.") for record in caplog.records)


def test_the_host_logger_can_be_replaced(caplog):
    host = PosixHost(
        executor_providers=(_Refusing("ssh"), ExecutorProvider("web", _execute))
    )
    host.logger = logging.getLogger("myapp")

    with caplog.at_level(logging.WARNING, logger="myapp"):
        host.run("true")

    (record,) = _fallback_warnings(caplog)
    assert record.name == "myapp"


# --- the audible fallback ------------------------------------------------------


def test_run_served_by_a_fallback_warns_once_per_provider(caplog):
    host = PosixHost(
        PosixConfig("nas1"),
        executor_providers=(_Refusing("ssh"), ExecutorProvider("webshell", _execute)),
    )

    with caplog.at_level(logging.WARNING, logger="hostctl"):
        host.run("true")
        host.run("true")

    (record,) = _fallback_warnings(caplog)
    assert record.getMessage() == (
        "[nas1] run() fell back from ssh to webshell: "
        "ssh: ssh refused: connection lost"
    )
    assert record.host == "nas1"


def test_a_failing_command_on_the_fallback_still_warns(caplog):
    def failing(command, *args, **options):
        raise subprocess.CalledProcessError(1, command)

    host = PosixHost(
        executor_providers=(_Refusing("ssh"), ExecutorProvider("web", failing))
    )

    with caplog.at_level(logging.WARNING, logger="hostctl"):
        with pytest.raises(subprocess.CalledProcessError):
            host.run("false")

    assert len(_fallback_warnings(caplog)) == 1


def test_an_unavailable_probe_is_a_fallback_too(caplog):
    offline = ExecutorProvider(
        "ssh", _execute, probe=lambda: ProviderProbe("unavailable", "no route")
    )
    host = PosixHost(executor_providers=(offline, ExecutorProvider("web", _execute)))

    with caplog.at_level(logging.WARNING, logger="hostctl"):
        host.run("true")

    (record,) = _fallback_warnings(caplog)
    assert "from ssh to web: ssh: no route" in record.getMessage()


def test_the_first_provider_serving_is_silent(caplog):
    host = PosixHost(
        executor_providers=(ExecutorProvider("ssh", _execute), _Refusing("web"))
    )

    with caplog.at_level(logging.WARNING, logger="hostctl"):
        host.run("true")

    assert _fallback_warnings(caplog) == []


class _RefusingPath(PathProvider):
    def __init__(self, name, root, **options):
        super().__init__(name, lambda *segments: Path(root, *segments), **options)

    def connect(self):
        raise OperationNotStartedError(f"{self.name} connection failed")


def test_a_path_operation_served_by_a_fallback_warns_once(tmp_path, caplog):
    (tmp_path / "f").write_bytes(b"data")
    host = PosixHost(
        PosixConfig("nas1"),
        path_providers=(
            _RefusingPath("sftp", tmp_path),
            PathProvider("tnasws", lambda *segments: Path(tmp_path, *segments)),
        ),
    )

    with caplog.at_level(logging.WARNING, logger="hostctl"):
        path = host.path("f")
        assert path.read_bytes() == b"data"
        assert path.read_bytes() == b"data"

    (record,) = _fallback_warnings(caplog)
    assert record.getMessage().startswith("[nas1] path() fell back from sftp to tnasws")


def test_routing_by_capability_and_pinning_are_not_fallbacks(tmp_path, caplog):
    (tmp_path / "f").write_bytes(b"data")
    readonly = PathProvider(
        "first",
        lambda *segments: Path(tmp_path, *segments),
        capabilities=("stat", "exists"),
    )
    full = PathProvider("second", lambda *segments: Path(tmp_path, *segments))
    host = PosixHost(path_providers=(readonly, full))

    with caplog.at_level(logging.WARNING, logger="hostctl"):
        assert host.path("f").read_bytes() == b"data"
        assert host.path("f", backend="second").read_bytes() == b"data"

    assert _fallback_warnings(caplog) == []
