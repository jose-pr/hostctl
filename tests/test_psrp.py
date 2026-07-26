"""Optional PSRP selection and runspace contract tests."""

from __future__ import annotations

import types

import pytest

from hostctl import HostConfig, RunspaceSession, WinRMConfig, WinRMHost
from hostctl.executor.psrp import pypsrp_available


def test_provider_uri_roundtrip_and_secret_safety():
    config = WinRMConfig("host", "user", "secret", provider="psrp")
    restored = HostConfig(str(config), password="secret")
    assert restored.provider == "psrp"
    assert "secret" not in str(config)


def test_explicit_psrp_has_actionable_error_when_missing(monkeypatch):
    monkeypatch.setattr("hostctl.host.winrm.pypsrp_available", lambda: False)
    host = WinRMHost(WinRMConfig("host", "user", provider="psrp"))
    with pytest.raises(ImportError, match="Python 3.10|psrp"):
        host.runspace()


def test_runspace_preserves_state_and_typed_streams(monkeypatch):
    class Streams:
        error = ["err"]
        warning = ["warn"]
        verbose = []
        debug = []
        information = []
        progress = []

    class Pipeline:
        streams = Streams()
        state = "Completed"
        had_errors = True

        def __init__(self, pool):
            self.pool = pool

        def add_script(self, script):
            self.script = script

        def invoke(self):
            self.pool.scripts.append(self.script)
            return [self.pool.scripts[-1]]

    class Pool:
        def __init__(self):
            self.scripts = []

        def open(self):
            self.opened = True

        def close(self):
            self.closed = True

    fake = types.ModuleType("pypsrp")
    fake_powershell = types.ModuleType("pypsrp.powershell")
    fake_powershell.PowerShell = Pipeline
    monkeypatch.setitem(__import__("sys").modules, "pypsrp", fake)
    monkeypatch.setitem(__import__("sys").modules, "pypsrp.powershell", fake_powershell)
    pool = Pool()
    session = RunspaceSession(pool=pool)
    first = session.invoke("$x = 1")
    second = session.invoke("$x")
    assert first.output == ("$x = 1",)
    assert second.output == ("$x",)
    assert first.streams.error == ("err",)
    assert first.had_errors and first.returncode == 1
    session.close()
