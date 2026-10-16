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


if os.environ.get("HOSTCTL_TEST_COLLECT") == "1":

    @pytest.fixture(autouse=True)
    def _collect_after_each_test():
        yield
        gc.collect()
