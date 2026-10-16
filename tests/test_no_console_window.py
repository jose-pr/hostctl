"""A child of a console-less process gets no window unless the caller asks."""

import os
import subprocess
import sys

import pytest

from hostctl import _windows
from hostctl.transports.local import LocalExecutor

windows = pytest.mark.skipif(os.name != "nt", reason="console windows are Windows")


def test_posix_never_sets_a_flag():
    if os.name == "nt":
        pytest.skip("POSIX behaviour")
    assert _windows.no_window_flags() == 0


@windows
@pytest.mark.parametrize(
    "console, expected", ((1, 0), (0, subprocess.CREATE_NO_WINDOW))
)
def test_the_flag_follows_whether_this_process_has_a_console(
    monkeypatch, console, expected
):
    import ctypes

    monkeypatch.setattr(
        ctypes.windll.kernel32, "GetConsoleWindow", lambda: console, raising=False
    )
    assert _windows.no_window_flags() == expected


def _recorded_flags(monkeypatch, **call):
    seen = []
    real = subprocess.Popen.__init__

    def record(self, *args, creationflags=0, **kwargs):
        seen.append(creationflags)
        real(self, *args, creationflags=creationflags, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", record)
    LocalExecutor()(sys.executable, "-c", "pass", **call)
    return seen


@windows
@pytest.mark.parametrize("timeout", (None, 30))
def test_the_local_executor_starts_children_without_a_window(monkeypatch, timeout):
    monkeypatch.setattr(
        _windows, "no_window_flags", lambda: subprocess.CREATE_NO_WINDOW
    )
    monkeypatch.setattr(
        "hostctl.transports.local._executor.no_window_flags",
        lambda: subprocess.CREATE_NO_WINDOW,
    )
    assert _recorded_flags(monkeypatch, timeout=timeout) == [
        subprocess.CREATE_NO_WINDOW
    ]


@windows
def test_an_explicit_creationflags_wins(monkeypatch):
    monkeypatch.setattr(
        "hostctl.transports.local._executor.no_window_flags",
        lambda: subprocess.CREATE_NO_WINDOW,
    )
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    assert _recorded_flags(monkeypatch, creationflags=flags) == [flags]


@windows
def test_the_native_winrm_relay_starts_without_a_window(monkeypatch):
    from hostctl.transports.winrm._native import NativeWinRMSession

    seen = []

    class _Completed:
        returncode, stdout, stderr = 0, b"", b""

    def fake_run(argv, **kwargs):
        seen.append(kwargs.get("creationflags", 0))
        return _Completed()

    monkeypatch.setattr(
        "hostctl.transports.winrm._native.no_window_flags",
        lambda: subprocess.CREATE_NO_WINDOW,
    )
    monkeypatch.setattr(subprocess, "run", fake_run)
    NativeWinRMSession("server.example").run_ps("Write-Output x")
    assert seen == [subprocess.CREATE_NO_WINDOW]
