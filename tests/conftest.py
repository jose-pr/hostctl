"""Shared test configuration.

Two things live here rather than in the modules that need them:

- The repository root goes on `sys.path`. `tests/test_application_provider.py`
  imports `examples.application_provider`, and `examples/` is deliberately not
  packaged. That import resolved only through `python -m pytest`'s injection of
  the current working directory, so invoking the plain `pytest` console script
  -- what editors, tox and pre-commit hooks use -- aborted collection of the
  WHOLE suite with `ModuleNotFoundError`, and nothing said why.
- `tests/` itself, so the handful of cross-module helper imports keep working
  under any import mode rather than only under pytest's default `prepend`.
- On Windows, every process the suite starts gets `CREATE_NO_WINDOW` when the
  run has no console (an editor or agent running pytest in the background), so
  the thousands of `cmd.exe`/`powershell.exe` children open no windows. A
  caller passing its own `creationflags` keeps them.
- No test reads the real `~/.ssh/config`. An SSH config's default is the
  user's own OpenSSH configuration, so every test resolves it to a file that
  does not exist; `fake_home` points `~` at a temporary directory and lets
  the default resolve there.
- With `HOSTCTL_TEST_COLLECT=1`, a full garbage collection after every test.
  An object left in a reference cycle (an unreaped subprocess, an unclosed
  socket) is otherwise collected during some later test, and the
  `ResourceWarning` fails that test instead of the one that leaked it.
"""

from __future__ import annotations

import gc
import os
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parent

for entry in (str(_ROOT), str(_TESTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)


def _windowless_popen() -> None:
    import ctypes
    import subprocess

    if ctypes.windll.kernel32.GetConsoleWindow():
        return
    original = subprocess.Popen.__init__

    def __init__(self, *args, creationflags=0, **kwargs):
        original(
            self,
            *args,
            creationflags=creationflags or subprocess.CREATE_NO_WINDOW,
            **kwargs,
        )

    subprocess.Popen.__init__ = __init__


if os.name == "nt":
    _windowless_popen()


_ABSENT_SSH_CONFIG = str(_TESTS / ".no-user-ssh-config" / "config")
_USER_SSH_CONFIG_PATH: dict = {}


@pytest.fixture(autouse=True)
def _no_user_ssh_config(monkeypatch):
    from hostctl.transports.ssh import _config_source

    _USER_SSH_CONFIG_PATH.setdefault("real", _config_source._user_ssh_config_path)
    monkeypatch.setattr(
        _config_source, "_user_ssh_config_path", lambda: _ABSENT_SSH_CONFIG
    )


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A home directory with an empty `.ssh/`, where `~` and the default
    ssh_config (`USER_SSH_CONFIG`) resolve for this test."""
    from hostctl.transports.ssh import _config_source

    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(
        _config_source, "_user_ssh_config_path", _USER_SSH_CONFIG_PATH["real"]
    )
    return home


if os.environ.get("HOSTCTL_TEST_COLLECT") == "1":

    @pytest.fixture(autouse=True)
    def _collect_after_each_test():
        yield
        gc.collect()
