"""hostctl's network-free performance surfaces.

Two things in this package run in a caller's hot path and touch nothing
outside the process, so they can be measured honestly on any machine:

- **shell rendering** -- every `run()` on every transport goes through
  `ShellFlavour.script()`/`command()`, and quoting is per-argument work;
- **composite path dispatch** -- `_CompositePathMixin` forwards each call
  through capability selection and provider pinning before the backend sees
  it, once per operation.

Run it:

    python benchmarks/run.py                 # print a table
    python benchmarks/run.py --save          # also write a results JSON

Each metric reports min/median/max milliseconds per call over N samples.
Compare on the **median**: a single average hides run-to-run noise, and this
runner deliberately does not try to quiet the machine it is on.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

RESULTS = Path(__file__).resolve().parent / "results"


def _measure(name, call, *, samples=7, iterations=2000):
    """Time `call` in batches, reporting ms per call."""
    call()  # warm the code paths and any lazy import
    timings = []
    for _ in range(samples):
        started = time.perf_counter()
        for _ in range(iterations):
            call()
        elapsed = time.perf_counter() - started
        timings.append(elapsed * 1000.0 / iterations)
    return {
        "name": name,
        "samples": samples,
        "iterations": iterations,
        "min_ms": min(timings),
        "median_ms": statistics.median(timings),
        "max_ms": max(timings),
    }


def _shell_cases():
    from hostctl.shell import CMD, POSIX_SHELL, POWERSHELL, PowerShellFlavour

    argv = ("tool.exe", "value with spaces", 'a "quoted" value', "%PATH%", "a&b")
    script = "printf '%s' hello; echo done"

    yield _measure("shell.posix.script(argv)", lambda: POSIX_SHELL.script((argv,)))
    yield _measure(
        "shell.posix.script(raw+cwd+env)",
        lambda: POSIX_SHELL.script((script,), cwd="/srv/app", env={"A": "1", "B": "2"}),
    )
    yield _measure(
        "shell.powershell5.structured_command",
        lambda: POWERSHELL.structured_command(argv),
    )
    yield _measure(
        "shell.powershell7.structured_command",
        lambda: PowerShellFlavour(7).structured_command(argv),
    )
    yield _measure(
        "shell.powershell.command(encoded)",
        lambda: POWERSHELL.command((argv,)),
        iterations=500,
    )
    yield _measure("shell.cmd.script(argv)", lambda: CMD.script((argv,)))


def _path_cases():
    from pathlib_next.mempath import MemPath, MemPathBackend
    from hostctl.host import PosixHost
    from hostctl.provider import PathProvider

    backend = MemPathBackend()
    MemPath("bench", backend=backend).mkdir()
    MemPath("bench/file.txt", backend=backend).write_bytes(b"payload")
    host = PosixHost(
        path_providers=(
            PathProvider(
                "mem",
                lambda *segments: MemPath(*segments, backend=backend),
                # The same set the composite tests use: the defaults plus
                # the three operations a fully capable backend adds.
                capabilities=PathProvider.DEFAULT_CAPABILITIES
                | {"symlink_to", "readlink", "scandir"},
            ),
        )
    )
    path = host.path("bench", "file.txt")
    directory = host.path("bench")

    yield _measure("path.build", lambda: host.path("bench", "file.txt"))
    yield _measure("path.exists", lambda: path.exists())
    yield _measure("path.read_bytes", lambda: path.read_bytes())
    yield _measure("path.iterdir", lambda: list(directory.iterdir()))
    yield _measure("path.truediv", lambda: directory / "file.txt")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--save",
        action="store_true",
        help="write benchmarks/results/<version>-py<major.minor>.json",
    )
    parser.add_argument(
        "--only",
        choices=("shell", "path"),
        help="run one group instead of both",
    )
    arguments = parser.parse_args(argv)

    import hostctl

    groups = {"shell": _shell_cases, "path": _path_cases}
    selected = [arguments.only] if arguments.only else sorted(groups)
    metrics = [metric for name in selected for metric in groups[name]()]

    width = max(len(metric["name"]) for metric in metrics)
    print(f"{'metric'.ljust(width)}   median ms      min      max")
    for metric in metrics:
        print(
            f"{metric['name'].ljust(width)}   "
            f"{metric['median_ms']:9.5f} "
            f"{metric['min_ms']:8.5f} "
            f"{metric['max_ms']:8.5f}"
        )

    if arguments.save:
        version = getattr(hostctl, "__version__", "unknown")
        interpreter = f"py{sys.version_info.major}.{sys.version_info.minor}"
        payload = {
            "name": f"hostctl {version} ({interpreter})",
            "package": "hostctl",
            "version": version,
            "interpreter": interpreter,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            # Keyed by metric NAME, not a list: that is the shape the
            # shared `compare_bench.py` reads, and a results file the
            # standard tool cannot load is a results file nobody compares.
            "metrics": {metric["name"]: metric for metric in metrics},
        }
        RESULTS.mkdir(parents=True, exist_ok=True)
        target = RESULTS / f"{version}-{interpreter}.json"
        # LF-only, on every platform. `write_text(newline=...)` is 3.10+,
        # and the default translates to `os.linesep` -- which would commit
        # CRLF from a Windows run.
        with open(target, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        print(f"\nwrote {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
