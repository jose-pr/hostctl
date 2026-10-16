# Deploying Python

Some hosts cannot install a Python program for themselves: no `pip`, a
read-only root filesystem, no route to a package index. They can still run a
*zipapp* — a zip of pure-Python packages with a `__main__.py`, which CPython
runs directly — or the same files unpacked as a plain tree. `hostctl.deploy`
builds either from what is installed on the machine running it and installs
it on the host through `host.run()` and `host.path()`, so it works over SSH, a
container, a guest agent or any other transport that reaches a POSIX host.

It needs the `deploy` extra:

```console
pip install "hostctl[deploy]"
```

## Deploying a distribution

```python
from hostctl import Exec, Host
from hostctl.deploy import deploy

with Host("ssh://admin@nas.example") as host:
    done = deploy(host, "mytool", "/opt/mytool.pyz")
    host.run(Exec(*done.command, "--version"))
```

`deploy()` does three things, each kept simple on purpose:

1. **What does the target already have?** It runs `python3 -` on the host
   with a short standard-library-only script that prints the installed
   distributions and the interpreter's
   [PEP 508](https://peps.python.org/pep-0508/) marker environment. It has to
   run before anything is installed, which is why it uses nothing else.
2. **What does `mytool` need?** Its dependency closure is read from this
   environment's installed metadata — the declared requirements, with extras
   and markers — not from an import scan, which cannot tell that
   `import yaml` means `pyyaml`. Markers are evaluated for the target, so a
   requirement gated on `sys_platform` follows the machine that will run it.
3. **Ship the difference.** Only what the target lacks is bundled. An
   appliance that already ships `requests` and its dependencies gets a payload
   of a few small packages.

The result says what happened:

```python
done.path      # '/opt/mytool.pyz'
done.digest    # SHA-256 of the payload
done.written   # False when the target already held this exact payload
done.command   # ('python3', '/opt/mytool.pyz'): run it with host.run(Exec(*done.command, ...))
```

`package=` names the import package to run when it differs from the
distribution name (`tnas-config` installing `tnasconfig`); `extras=` adds the
distribution's extras; `skip=` leaves out distributions you install on the
target by other means; `python=` names the target interpreter
(`"/usr/local/bin/python3.12"`).

## Two layouts

| `mode` | what lands at `path` | runs as |
| --- | --- | --- |
| `"pyz"` (default) | one executable zipapp file | `python3 PATH` |
| `"dir"` | `lib/` with the packages, `bin/<package>` launcher | `PATH/bin/<package>` |

A zipapp is one file to copy, verify and replace. A tree is readable on the
target: editable in place, greppable, able to carry data files. Its launcher
is a `/bin/sh` script that puts `lib` ahead of any inherited `PYTHONPATH` and
runs `python3 -m <package>`.

Both carry each bundled distribution's `.dist-info`, so
`importlib.metadata.version()` — and a `--version` flag built on it — works
in the deployed copy.

## Safe to repeat

Payloads are reproducible: every entry gets the same timestamp, mode and
owner, so building the same inputs twice gives the same bytes. The digest is
recorded on the target (`PATH.digest` for a zipapp, `PATH/.digest` inside a
tree), and a deploy that finds its own digest there writes nothing. Pass
`force=True` to write anyway.

Installing is atomic. A zipapp is written to `PATH.new` and renamed over
`PATH`, so a process already running it never reads a half-written file. A
tree is uploaded as one tar, unpacked into `PATH.new` with its digest inside,
and swapped in by renaming the old tree to `PATH.old`; if a deploy is killed
between those two renames, the next one puts `PATH.old` back before doing
anything else.

`deploy()` refuses to replace a `PATH` that exists without the digest marker,
`force` or not: it is not something a deploy wrote, and replacing it would
delete it. Choose another path or remove it yourself.

## Shipping a working tree

A `SourceTree` ships a checkout as it is, filtered the way a clone of it
would be:

```python
from hostctl.deploy import SourceTree, deploy

tree = SourceTree(".", ignore_patterns=["*.log"])
deploy(host, tree, "/opt/mytool", mode="dir", package="mytool")
```

- Files are filtered by `.gitignore`, `.ignore` and `.bundleignore`, with
  git's rules: a later `!pattern` re-includes, a nested ignore file applies
  below its own directory. `ignore_files=` names which to read (one that is
  missing is an error); `ignore_patterns=` are applied last.
- `.git` directories, at any depth, and symbolic links never ship.
- The launcher finds the package under `src/`, `lib/`, `vendor/` or the
  tree's root; `pythonpath=` names the directories instead.
- The tree's own dependencies are **not** vendored — resolving them needs
  them installed, which is what a tree source avoids. `deploy()` logs what
  the tree declares so you can check the target has it, and
  `tree.requirements()` returns the same names.
- A tree ships only as `mode="dir"`: a zipapp needs the package at the
  archive root, which a `src/` layout does not have.

## Building without a host

`bundle()` writes the same layouts locally:

```python
from hostctl.deploy import bundle

bundle("mytool", "dist/mytool.pyz")                 # one zipapp
bundle("mytool", "dist/mytool", mode="dir")         # lib/ and bin/mytool
```

With no target to ask, the whole closure ships, markers evaluated for the
interpreter running `bundle()`.

## Limits

- The target must be POSIX, with `mv`, `rm`, `mkdir`, `chmod` and a `tar`
  that takes `--strip-components` (GNU tar and bsdtar do). A Windows host
  raises `NotImplementedError` before anything is sent.
- A distribution containing a compiled extension (`.so`, `.pyd`) is refused
  with `BundleError`: it is built for this interpreter and platform and would
  fail to import on the target. Install it there another way and pass it in
  `skip=`.
- A step that fails on the target raises `subprocess.CalledProcessError`.
  The previous deploy stays at `PATH`, or, when the failure fell between the
  two renames of a tree, at `PATH.old`, where the next deploy restores it.
