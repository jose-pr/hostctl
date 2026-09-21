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
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parent

for entry in (str(_ROOT), str(_TESTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)
