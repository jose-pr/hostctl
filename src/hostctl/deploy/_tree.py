"""A working tree copied as a clone would have it, and what it declares.

Files are filtered by the tree's own ignore files with gitignore semantics
(``pathspec``); declared dependency names come from ``pyproject.toml`` or
``requirements.txt`` without importing or installing anything.
"""

from __future__ import annotations

import logging
import typing
from pathlib import Path

from .._extras import _require
from ..exceptions import BundleError, MissingExtraError

_LOGGER = logging.getLogger(__name__)

#: The ignore files read by default, in the order their patterns are layered:
#: a later file's `!negation` can re-include what an earlier one ignored.
DEFAULT_IGNORE_FILES = (".gitignore", ".ignore", ".bundleignore")

#: pathspec's `GitIgnoreBasicPattern`; `"gitwildmatch"` is deprecated from
#: pathspec 1.0.
_PATTERN_FACTORY = "gitignore"

#: Excluded at any depth whatever an ignore file says: a repository's own, or
#: a nested checkout's, history is never part of what a clone has.
_ALWAYS_IGNORED = (".git",)

#: Checked in this order for an importable package when no PYTHONPATH entry
#: is named: the `src/` layout, vendored trees, and the flat layout.
PYTHONPATH_CANDIDATES = ("src", "lib", "vendor", "")


def _pathspec() -> typing.Any:
    _require("deploy", "a SourceTree")
    import pathspec

    return pathspec


def _read_lines(path: Path) -> typing.List[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _load_ignore(
    root: Path,
    files: typing.Optional[typing.Sequence[str]],
    extra_patterns: typing.Sequence[str],
) -> typing.Any:
    """One ``PathSpec`` from the root's ignore files, then ``extra_patterns``.

    A default ignore file that is absent is skipped; one the caller named and
    that is absent raises, since the filtering asked for would not happen.
    ``extra_patterns`` are a separate spec added last, so their negations can
    re-include what a file ignored.
    """
    pathspec = _pathspec()
    lines: typing.List[str] = []
    for name in files if files is not None else DEFAULT_IGNORE_FILES:
        path = root / name
        if path.is_file():
            lines.extend(_read_lines(path))
        elif files is not None:
            raise BundleError(
                f"ignore file {name!r} not found in {root}: nothing would be "
                "filtered by it"
            )
    spec = pathspec.PathSpec.from_lines(_PATTERN_FACTORY, lines)
    if extra_patterns:
        spec = spec + pathspec.PathSpec.from_lines(_PATTERN_FACTORY, extra_patterns)
    return spec


def _extend_ignore(
    spec: typing.Any,
    directory: Path,
    files: typing.Optional[typing.Sequence[str]],
    root: Path,
) -> typing.Any:
    """``spec`` plus the ignore files in ``directory``, anchored there as git
    anchors a nested ``.gitignore``: ``build/`` in ``src/`` means ``src/build``."""
    if directory == root:
        return spec
    lines: typing.List[str] = []
    for name in files if files is not None else DEFAULT_IGNORE_FILES:
        path = directory / name
        if path.is_file():
            lines.extend(_read_lines(path))
    if not lines:
        return spec
    relative = directory.relative_to(root).as_posix()
    anchored = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        negated = stripped.startswith("!")
        body = stripped[1:] if negated else stripped
        body = body[1:] if body.startswith("/") else body
        anchored.append(("!" if negated else "") + f"{relative}/{body}")
    return spec + _pathspec().PathSpec.from_lines(_PATTERN_FACTORY, anchored)


def collect_tree(
    root: typing.Union[str, Path],
    *,
    ignore_files: typing.Optional[typing.Sequence[str]] = None,
    extra_ignores: typing.Sequence[str] = (),
    prefix: typing.Optional[str] = None,
) -> typing.List[typing.Tuple[str, Path]]:
    """``[(arcname, source)]`` for a tree, filtered as a clone would be.

    Arcnames sit under one top directory, ``prefix`` (default: the resolved
    root's name). A directory an ignore pattern matches is pruned before it is
    walked, which is what a directory pattern such as ``.venv*/`` needs.
    Symbolic links are skipped, never followed: a link out of the tree would
    ship whatever it points at, and a loop would never end.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise BundleError(f"{root} is not a directory")
    spec = _load_ignore(root, ignore_files, extra_ignores)
    top = prefix if prefix is not None else root.name

    def ignored(relative: str, is_dir: bool, spec: typing.Any) -> bool:
        if any(part in _ALWAYS_IGNORED for part in Path(relative).parts):
            return True
        return spec.match_file(relative + "/" if is_dir else relative)

    contents: typing.List[typing.Tuple[str, Path]] = []

    def walk(directory: Path, spec: typing.Any) -> None:
        spec = _extend_ignore(spec, directory, ignore_files, root)
        for child in sorted(directory.iterdir()):
            relative = child.relative_to(root).as_posix()
            if child.is_symlink():
                _LOGGER.debug("skipping symlink %s", relative)
                continue
            if child.is_dir():
                if not ignored(relative, True, spec):
                    walk(child, spec)
            elif child.is_file():
                if not ignored(relative, False, spec):
                    contents.append((f"{top}/{relative}", child))

    walk(root, spec)
    return contents


def detect_pythonpath(root: Path, package: str) -> typing.List[str]:
    """Every :data:`PYTHONPATH_CANDIDATES` entry holding ``package``.

    A directory counts when it has ``<package>/__init__.py`` or
    ``<package>.py``. Every match is returned: a tree that vendors under
    ``lib/`` while its own code is in ``src/`` needs both.
    """
    found: typing.List[str] = []
    for candidate in PYTHONPATH_CANDIDATES:
        base = root / candidate if candidate else root
        if (base / package / "__init__.py").is_file() or (
            base / f"{package}.py"
        ).is_file():
            found.append(candidate)
    return found


def _toml_loads() -> typing.Callable[[str], typing.Dict[str, typing.Any]]:
    try:
        import tomllib

        return tomllib.loads
    except ImportError:
        pass
    try:
        import tomli
    except ImportError as exc:
        raise MissingExtraError(
            "deploy", "reading pyproject.toml before Python 3.11"
        ) from exc
    return tomli.loads


def _dependency_name(raw: str) -> str:
    from packaging.requirements import Requirement

    return Requirement(raw).name


def _parse_pyproject(
    text: str, extras: typing.Sequence[str], *, include_base: bool = True
) -> typing.List[str]:
    """Names from ``[project] dependencies`` and the requested extras.

    ``include_base=False`` gives what the extras alone add, which is what an
    ``[extra]`` include or exclude means.
    """
    project = _toml_loads()(text).get("project", {})
    names = (
        [_dependency_name(raw) for raw in project.get("dependencies", [])]
        if include_base
        else []
    )
    optional = project.get("optional-dependencies", {})
    for extra in extras:
        names.extend(_dependency_name(raw) for raw in optional.get(extra, []))
    return names


def _parse_requirements_txt(text: str) -> typing.List[str]:
    """Names from a ``requirements.txt``: comments, blank lines, backslash
    continuations and inline comments handled; option lines (``-r``, ``-e``,
    ``-c``) skipped; a line no parser accepts is logged and skipped."""
    from packaging.requirements import InvalidRequirement

    joined: typing.List[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = buffer + raw.rstrip()
        if line.endswith("\\"):
            buffer = line[:-1]
            continue
        buffer = ""
        joined.append(line)
    if buffer:
        joined.append(buffer)
    names: typing.List[str] = []
    for line in joined:
        line = line.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        try:
            names.append(_dependency_name(line))
        except InvalidRequirement as exc:
            _LOGGER.warning("ignoring unparseable requirement %r: %s", line, exc)
    return names


def tree_requirements(
    root: typing.Union[str, Path],
    extras: typing.Sequence[str] = (),
    *,
    include: typing.Sequence[str] = (),
    exclude: typing.Sequence[str] = (),
) -> typing.List[str]:
    """Declared dependency names of a tree, read statically.

    ``pyproject.toml`` wins over ``requirements.txt``. ``include`` and
    ``exclude`` take a name or a bracketed ``[extra]``; ``exclude`` is applied
    last and wins. A bracketed extra means nothing for a requirements file.
    """
    _require("deploy", "reading declared requirements")
    root = Path(root)
    pyproject = root / "pyproject.toml"
    listing = root / "requirements.txt"
    if pyproject.is_file():
        source = pyproject.read_text(encoding="utf-8")
        names = _parse_pyproject(source, extras)
    elif listing.is_file():
        source = None
        names = _parse_requirements_txt(listing.read_text(encoding="utf-8"))
    else:
        raise BundleError(f"no pyproject.toml or requirements.txt found under {root}")

    def extra_names(extra: str) -> typing.List[str]:
        if source is None:
            return []
        return _parse_pyproject(source, (extra,), include_base=False)

    result = list(dict.fromkeys(names))
    for item in include:
        if item.startswith("[") and item.endswith("]"):
            for name in extra_names(item[1:-1]):
                if name not in result:
                    result.append(name)
        elif item not in result:
            result.append(item)

    excluded = {item for item in exclude if not item.startswith("[")}
    for item in exclude:
        if item.startswith("[") and item.endswith("]"):
            excluded.update(extra_names(item[1:-1]))
    return [name for name in result if name not in excluded]
