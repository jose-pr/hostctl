"""Optional PSRP selection and runspace contract tests."""

from __future__ import annotations

import types

import pytest

from hostctl import HostConfig, RunspaceSession, WinRMConfig, WinRMHost
from hostctl.executor.psrp import PsrpExecutor, pypsrp_available
from hostctl.process.psrp import PipelineResult, PipelineStreams


def test_provider_uri_roundtrip_and_secret_safety():
    config = WinRMConfig("host", "user", "secret", provider="psrp")
    restored = HostConfig(str(config), password="secret")
    assert restored.provider == "psrp"
    assert "secret" not in str(config)


def test_explicit_psrp_has_actionable_error_when_missing(monkeypatch):
    monkeypatch.setattr("hostctl.host._winrm.pypsrp_available", lambda: False)
    host = WinRMHost(WinRMConfig("host", "user", provider="psrp"))
    with pytest.raises(ImportError, match="Python 3.10|psrp"):
        host.runspace()


@pytest.mark.parametrize(
    ("state", "had_errors", "returncode"),
    [("Completed", False, 0), ("Failed", False, 1), ("Stopped", False, 1)],
)
def test_runspace_preserves_state_and_typed_streams(
    monkeypatch, state, had_errors, returncode
):
    class Streams:
        error = ["err"]
        warning = ["warn"]
        verbose = ["verbose"]
        debug = ["debug"]
        information = ["info"]
        progress = ["progress"]

    class Pipeline:
        streams = Streams()

        def __init__(self, pool):
            self.pool = pool
            self.state = state
            self.had_errors = had_errors

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
    assert first.streams.warning == ("warn",)
    assert first.streams.verbose == ("verbose",)
    assert first.streams.debug == ("debug",)
    assert first.streams.information == ("info",)
    assert first.streams.progress == ("progress",)
    assert first.returncode == returncode
    session.close()


def test_psrp_executor_projects_objects_and_error_streams():
    class Session:
        def invoke(self, script, *, raw=False):
            assert script == "Write-Output x"
            return PipelineResult(
                ("one", "two"),
                PipelineStreams(error=("bad",)),
                "Completed",
                True,
                1,
            )

    result = PsrpExecutor(lambda: Session())("Write-Output x", check=False, text=True)
    assert result.stdout == "one\ntwo\n"
    assert result.stderr == "bad"
    assert result.returncode == 1


def test_psrp_executor_rejects_byte_stream_options():
    executor = PsrpExecutor(lambda: object())
    with pytest.raises(NotImplementedError, match="stdin"):
        executor("x", input=b"x")
    with pytest.raises(NotImplementedError, match="timeout"):
        executor("x", timeout=1)
