"""Which installed distributions a bundle carries, and which files they are.

The closure is read from installed distribution metadata, not from the
source: declared dependencies already account for extras and environment
markers, and an import scan cannot see that ``import yaml`` means ``pyyaml``
or a lazy import inside a function body.
"""

from __future__ import annotations

import json
import typing
from pathlib import Path

from ..exceptions import BundleError

#: Distributions never worth shipping: packaging tooling every interpreter
#: that can run the probe already has.
_NEVER_BUNDLE = frozenset({"pip", "setuptools", "wheel", "pkg-resources"})

_NATIVE_SUFFIXES = frozenset({".so", ".pyd", ".dll"})

#: Stdlib-only source run on the target with ``python -``, before anything is
#: installed there. The first line is a JSON object holding the target's PEP 508
#: marker environment, built by hand because the target need not have
#: ``packaging``; every later line is one normalized distribution name.
PROBE_SOURCE = """\
import json
import os
import platform
import sys
try:
    from importlib.metadata import distributions
except ImportError:
    sys.exit("probe requires Python 3.8+")
version = ".".join(str(p) for p in sys.version_info[:3])
print(json.dumps({
    "__marker_environment__": {
        "implementation_name": sys.implementation.name,
        "implementation_version": version,
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": platform.python_version(),
        "python_version": ".".join(str(p) for p in sys.version_info[:2]),
        "sys_platform": sys.platform,
    }
}))
seen = set()
for dist in distributions():
    name = (dist.metadata["Name"] or "").strip()
    if name:
        seen.add(name.lower().replace("_", "-"))
for name in sorted(seen):
    print(name)
"""


def canonical(name: str) -> str:
    """PEP 503 normalization, enough for matching distribution names."""
    return name.lower().replace("_", "-").replace(".", "-")


def default_package(distribution: str) -> str:
    """The import name assumed for ``distribution``: ``my-tool`` -> ``my_tool``.

    The two can differ (a ``tnas-config`` distribution may install
    ``tnasconfig``), which is why callers can name the package separately.
    """
    return distribution.replace("-", "_")


def parse_probe(
    output: typing.Union[str, typing.Iterable[str]],
) -> typing.Tuple[typing.List[str], typing.Dict[str, str]]:
    """``(names, marker_environment)`` from what :data:`PROBE_SOURCE` printed.

    The environment line is optional: output that is names only yields an
    empty environment, and markers are then evaluated against this machine.
    """
    lines = output.splitlines() if isinstance(output, str) else list(output)
    environment: typing.Dict[str, str] = {}
    names: typing.List[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not environment and line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                payload = {}
            if isinstance(payload, dict) and "__marker_environment__" in payload:
                environment = payload["__marker_environment__"] or {}
                continue
        names.append(line)
    return names, environment


def requirements(
    root: str,
    extras: typing.Sequence[str] = (),
    environment: typing.Optional[typing.Mapping[str, str]] = None,
) -> typing.Dict[str, typing.Any]:
    """``root``'s transitive dependency closure: ``{canonical name: Distribution}``.

    ``root`` itself is included. A declared dependency that is not installed
    here is skipped: the closure is what this environment can contribute.
    ``extras`` selects ``root``'s declared extras; a dependency's own extras
    (``hostctl[ssh]``) are followed for that dependency only. Markers are
    evaluated against ``environment`` (the target's, from the probe) merged
    over this interpreter's.
    """
    from importlib.metadata import PackageNotFoundError, distribution

    from packaging.requirements import Requirement

    found: typing.Dict[str, typing.Any] = {}
    pending: typing.List[typing.Tuple[str, typing.Tuple[str, ...]]] = [
        (root, tuple(extras))
    ]
    seen: typing.Set[typing.Tuple[str, typing.Tuple[str, ...]]] = set()
    while pending:
        name, wanted = pending.pop()
        key = canonical(name)
        if (key, wanted) in seen or key in _NEVER_BUNDLE:
            continue
        seen.add((key, wanted))
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            continue
        found.setdefault(key, dist)
        for raw in dist.requires or []:
            requirement = Requirement(raw)
            marker = requirement.marker
            if marker is not None:
                # An extra-gated requirement counts only when this distribution
                # was reached with that extra; `extra=""` answers the plain
                # environment markers.
                contexts = [{"extra": extra} for extra in wanted]
                contexts.append({"extra": ""})
                if not any(
                    marker.evaluate({**dict(environment or {}), **context})
                    for context in contexts
                ):
                    continue
            pending.append((requirement.name, tuple(requirement.extras)))
    return found


def missing_on(
    installed: typing.Iterable[str],
    root: str,
    extras: typing.Sequence[str] = (),
    skip: typing.Iterable[str] = (),
) -> typing.Dict[str, typing.Any]:
    """The closure entries the target does not already provide.

    ``installed`` is the probe's output lines; names compare canonically, so
    ``pathapi_uri`` and ``pathapi-uri`` are one distribution. ``skip`` names
    distributions never to ship even when the target lacks them.
    """
    names, environment = parse_probe(list(installed))
    have = {canonical(name) for name in names}
    have.update(canonical(name) for name in skip)
    return {
        key: dist
        for key, dist in requirements(root, extras, environment).items()
        if key not in have
    }


def _top_level_paths(dist: typing.Any) -> typing.List[Path]:
    """The importable files and directories a distribution installs."""
    base = getattr(dist, "_path", None)
    anchor = Path(base).parent if base else None
    tops: typing.Set[str] = set()

    text = dist.read_text("top_level.txt")
    if text:
        tops.update(line.strip() for line in text.splitlines() if line.strip())
    if not tops:
        for file in dist.files or []:
            parts = Path(str(file)).parts
            if not parts:
                continue
            first = parts[0]
            # RECORD paths are relative to site-packages and may leave it (a
            # console script is `../../bin/foo`); `..` would make the whole
            # parent tree look like part of this distribution.
            if first in {"..", "."} or first.endswith(
                (".dist-info", ".egg-info", ".pth")
            ):
                continue
            tops.add(first[:-3] if first.endswith(".py") else first)

    paths: typing.List[Path] = []
    for top in sorted(tops):
        if anchor is not None:
            package = anchor / top
            module = anchor / f"{top}.py"
            # A directory without `__init__.py` may be what an uninstall left
            # behind; taking it would ship a package that cannot be imported.
            if (package / "__init__.py").is_file():
                paths.append(package)
                continue
            if module.is_file():
                paths.append(module)
                continue
        # An editable install's RECORD lists only a `.pth` shim; the package
        # lives where the import system finds it.
        located = _locate_by_import(top)
        if located is not None:
            paths.append(located)
    return paths


def _locate_by_import(top: str) -> typing.Optional[Path]:
    """Where a top-level name is imported from, without importing it."""
    import importlib.util

    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin or spec.origin in ("built-in", "frozen"):
        return None
    origin = Path(spec.origin)
    return origin.parent if origin.name == "__init__.py" else origin


def _reject_native(name: str, paths: typing.Iterable[Path]) -> None:
    """Refuse a distribution carrying a compiled extension.

    An extension is built for one interpreter and platform; shipped to
    another, it fails at import, long after anything could say why.
    """
    for path in paths:
        if path.is_dir():
            native = [
                str(child.relative_to(path))
                for child in path.rglob("*")
                if child.suffix in _NATIVE_SUFFIXES
            ]
        else:
            native = [path.name] if path.suffix in _NATIVE_SUFFIXES else []
        if native:
            raise BundleError(
                f"{name!r} contains a compiled extension ({native[0]}) and cannot "
                "be bundled: it is built for this interpreter and platform. "
                "Install it on the target by other means and leave it out with "
                "skip"
            )


def collect(
    distributions: typing.Mapping[str, typing.Any],
) -> typing.List[typing.Tuple[str, Path]]:
    """``[(arcname, source)]`` for every file the distributions contribute.

    ``__pycache__`` and loose ``.pyc``/``.pyo`` files are left out: they
    belong to this interpreter's version.
    """
    contents: typing.List[typing.Tuple[str, Path]] = []
    for name, dist in sorted(distributions.items()):
        paths = _top_level_paths(dist)
        if not paths:
            raise BundleError(
                f"cannot locate the installed files for {name!r}: its metadata "
                "lists no importable top-level package, and none could be "
                "imported by name"
            )
        if not any(path.is_dir() or path.suffix == ".py" for path in paths):
            raise BundleError(
                f"{name!r} resolved to data files only ({paths[0].name}) with no "
                "importable module; the bundle would not be able to import it"
            )
        _reject_native(name, paths)
        for path in paths:
            if path.is_dir():
                for child in sorted(path.rglob("*")):
                    if not child.is_file() or "__pycache__" in child.parts:
                        continue
                    if child.suffix in {".pyc", ".pyo"}:
                        continue
                    contents.append(
                        (f"{path.name}/{child.relative_to(path).as_posix()}", child)
                    )
            else:
                contents.append((path.name, path))
    return contents


#: Written into each bundled `.dist-info`, naming what put it there.
_INSTALLER = b"hostctl-deploy\n"


def metadata_entries(
    distributions: typing.Mapping[str, typing.Any],
) -> typing.List[typing.Tuple[str, bytes]]:
    """``[(arcname, contents)]`` of a ``.dist-info`` per bundled distribution.

    ``importlib.metadata`` reads a ``.dist-info`` inside a zip on ``sys.path``
    (3.9 and 3.14 alike), so a deployed copy answers ``version()``. The real
    ``METADATA`` is copied when readable; otherwise a minimal ``Name``/
    ``Version`` file is written. A distribution with no version is skipped.
    """
    entries: typing.List[typing.Tuple[str, bytes]] = []
    for dist in distributions.values():
        name = _dist_name(dist)
        version = _dist_version(dist)
        if not name or not version:
            continue
        # The PEP 427 directory name uses the underscored form.
        folder = f"{name.replace('-', '_')}-{version}.dist-info"
        try:
            text = dist.read_text("METADATA")
        except (OSError, ValueError):
            text = None
        if not text:
            text = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        entries.append((f"{folder}/METADATA", text.encode("utf-8")))
        entries.append((f"{folder}/INSTALLER", _INSTALLER))
    return sorted(entries)


def _dist_name(dist: typing.Any) -> str:
    metadata = getattr(dist, "metadata", None)
    name = ""
    if metadata is not None:
        name = (metadata["Name"] or "").strip()
    return name or str(getattr(dist, "name", "") or "")


def _dist_version(dist: typing.Any) -> str:
    version = getattr(dist, "version", "") or ""
    if version:
        return str(version)
    metadata = getattr(dist, "metadata", None)
    if metadata is not None:
        return (metadata["Version"] or "").strip()
    return ""
