# Benchmarks

Two network-free surfaces, because they are the only ones that can be measured
honestly on an arbitrary machine:

- **shell rendering** — every `run()` on every transport goes through
  `ShellFlavour.script()`/`command()`, and quoting is per-argument work;
- **composite path dispatch** — `_CompositePathMixin` forwards each call
  through capability selection and provider pinning before a backend sees it,
  once per operation, against an in-memory backend.

Anything involving a socket, a subprocess or a disk is deliberately absent: it
would measure the machine, not this package.

## Reproduce

```bash
python benchmarks/run.py                     # print the table
python benchmarks/run.py --save              # also write a results JSON
python benchmarks/run.py --only shell        # one group
```

Run it on demand. It is not wired into CI: per-push perf numbers from shared
runners are noise, and a number that moves for reasons nobody can attribute is
worse than no number.

## Results

One file per `(version, interpreter)` at
`benchmarks/results/<version>-py<major>.<minor>.json`, **tracked and
committed** — that is what makes a before/after recoverable later. Schema:

```json
{
  "package": "hostctl",
  "version": "0.2.7",
  "interpreter": "py3.14",
  "python": "3.14.6",
  "platform": "Windows-11-...",
  "machine": "ARM64",
  "metrics": [
    {
      "name": "shell.posix.script(argv)",
      "samples": 7,
      "iterations": 2000,
      "min_ms": 0.0104,
      "median_ms": 0.0105,
      "max_ms": 0.0116
    }
  ]
}
```

Each metric times `iterations` calls in a batch and repeats that `samples`
times, reporting **milliseconds per call**.

## Reading them

**Compare on the median.** A single average hides run-to-run noise, and this
runner makes no attempt to quiet the machine it runs on — no pinning, no
priority change, no turbo control. Treat a difference under ~10% between two
runs on the same machine as noise, and a difference between two machines as
meaningless.

A local number is a sanity check, not evidence. A perf claim in a release or a
changelog comes from a run whose conditions are stated.

The committed `0.2.7` files are the **baseline**: the state at the end of the
2026-09-20 review remediation, before any optimisation work. Nothing in this
package has been tuned for speed yet, which is exactly why a baseline is worth
having now.
