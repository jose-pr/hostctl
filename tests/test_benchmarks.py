"""The benchmark runner is not run in CI, so nothing else notices it rotting.

`benchmarks/run.py` is run on demand and its results are tracked. A metric
renamed or dropped without saving new results breaks the comparison with every
tracked baseline, and a broken import is found only when someone next
wants a number.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "benchmarks" / "run.py"
RESULTS = ROOT / "benchmarks" / "results"


@pytest.fixture
def runner(monkeypatch):
    if not RUNNER.is_file():
        pytest.skip("benchmarks/ is not part of this tree")
    # The runner puts `src` and `tests` on `sys.path` as it loads.
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("_hostctl_benchmark_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _newest_results():
    """The results files of the newest saved version, one per interpreter."""
    by_version = {}
    for path in RESULTS.glob("*-py*.json"):
        by_version.setdefault(Version(path.stem.partition("-py")[0]), []).append(path)
    return by_version[max(by_version)]


def test_the_runner_imports(runner):
    assert callable(runner.main)
    assert callable(runner._shell_cases)
    assert callable(runner._path_cases)


def test_metric_names_match_the_newest_results(runner):
    # The stub still calls the measured code once, so a case that cannot
    # run fails here and not on the next benchmark run.
    runner._measure = lambda name, call, **_: (call(), {"name": name})[1]
    names = {m["name"] for m in [*runner._shell_cases(), *runner._path_cases()]}

    saved = _newest_results()
    assert saved, "benchmarks/results/ holds no results"
    for path in saved:
        recorded = set(json.loads(path.read_text(encoding="utf-8"))["metrics"])
        assert names == recorded, (
            f"the runner's metrics differ from {path.name}; if a metric was "
            "renamed or removed on purpose, save new results with "
            "`python benchmarks/run.py --save`"
        )
