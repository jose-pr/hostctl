"""Install a Python program on a host that cannot install it for itself.

A host with no ``pip``, a read-only root or no outbound network can still run
a zipapp, or the same files unpacked as a tree. :func:`deploy` asks the
target's interpreter which distributions it already has, bundles only the
difference from this environment's installed metadata, and installs the
result atomically through ``host.run()`` and ``host.path()``, so it works over
any transport to a POSIX host. :func:`bundle` writes the same layouts locally.

Needs the ``deploy`` extra (``pip install "hostctl[deploy]"``); importing this
module does not, and the first call without it raises `MissingExtraError` naming it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import tempfile
import typing
from pathlib import Path, PurePosixPath

from .._extras import _require
from ..exceptions import BundleError

if typing.TYPE_CHECKING:
    from ..host._host import Host

__all__ = ["Deployment", "SourceTree", "bundle", "deploy"]

_LOGGER = logging.getLogger(__name__)
_MODES = ("pyz", "dir")


@dataclasses.dataclass(frozen=True, init=False)
class SourceTree:
    """A working tree to ship as-is, filtered as a clone of it would be.

    Its own dependencies are not vendored: that needs them installed here,
    which a tree source exists to avoid. A tree ships only as ``mode="dir"``.
    """

    root: Path
    ignore_files: typing.Optional[typing.Tuple[str, ...]]
    ignore_patterns: typing.Tuple[str, ...]
    pythonpath: typing.Optional[typing.Tuple[str, ...]]

    def __init__(
        self,
        root: typing.Union[str, "os.PathLike[str]"],
        *,
        ignore_files: typing.Optional[typing.Iterable[str]] = None,
        ignore_patterns: typing.Iterable[str] = (),
        pythonpath: typing.Optional[typing.Iterable[str]] = None,
    ) -> None:
        object.__setattr__(self, "root", Path(root))
        object.__setattr__(
            self, "ignore_files", None if ignore_files is None else tuple(ignore_files)
        )
        object.__setattr__(self, "ignore_patterns", tuple(ignore_patterns))
        object.__setattr__(
            self, "pythonpath", None if pythonpath is None else tuple(pythonpath)
        )

    def files(self) -> typing.List[typing.Tuple[str, Path]]:
        """``[(arcname, source)]``, each arcname under the root's own name."""
        from ._tree import collect_tree

        return collect_tree(
            self.root,
            ignore_files=self.ignore_files,
            extra_ignores=self.ignore_patterns,
        )

    def requirements(
        self,
        extras: typing.Sequence[str] = (),
        *,
        include: typing.Sequence[str] = (),
        exclude: typing.Sequence[str] = (),
    ) -> typing.List[str]:
        """The dependency names the tree declares, read without installing.

        From ``pyproject.toml`` (``dependencies`` plus each of ``extras``),
        else ``requirements.txt``. ``include``/``exclude`` take a name or a
        bracketed ``"[extra]"``; ``exclude`` wins.
        """
        from ._tree import tree_requirements

        return tree_requirements(
            self.root, tuple(extras), include=tuple(include), exclude=tuple(exclude)
        )

    def _import_roots(self, package: str) -> typing.List[str]:
        """The tree's import roots for ``package``, relative to ``lib/``."""
        from ._tree import PYTHONPATH_CANDIDATES, detect_pythonpath

        resolved = self.root.resolve()
        entries = list(self.pythonpath or ())
        if self.pythonpath is None:
            entries = detect_pythonpath(resolved, package)
            if not entries:
                checked = ", ".join(c or "." for c in PYTHONPATH_CANDIDATES)
                raise BundleError(
                    f"could not find an importable {package!r} under {resolved} "
                    f"(checked {checked}); name its directory in pythonpath"
                )
        return [
            f"{resolved.name}/{entry}" if entry else resolved.name for entry in entries
        ]


@dataclasses.dataclass(frozen=True)
class Deployment:
    """What :func:`deploy` did.

    ``command`` is the argv that runs the deployed program on the target:
    ``host.run(Exec(*deployment.command, *args))``.
    """

    path: str
    digest: str
    written: bool
    command: typing.Tuple[str, ...]


class _Payload(typing.NamedTuple):
    contents: typing.List[typing.Tuple[str, Path]]
    metadata: typing.List[typing.Tuple[str, bytes]]
    roots: typing.List[str]


def _package(source: typing.Union[str, SourceTree], package: typing.Optional[str]):
    from ._closure import default_package

    if package is None:
        if isinstance(source, SourceTree):
            raise TypeError("a SourceTree needs package= (the import name to run)")
        package = default_package(source)
    if not package or not all(part.isidentifier() for part in package.split(".")):
        raise ValueError(f"package must be a dotted import name: {package!r}")
    return package


def _check(source: typing.Union[str, SourceTree], mode: str) -> None:
    if mode not in _MODES:
        raise ValueError(f"mode must be 'pyz' or 'dir': {mode!r}")
    if isinstance(source, SourceTree) and mode == "pyz":
        # zipimport needs the package at the archive root; a tree keeps its
        # own src/ or lib/ layout, which only the tree launcher can reach.
        raise BundleError(
            "a SourceTree ships only as mode='dir': a zipapp needs the package "
            "at the archive root, which a src/-layout tree does not have"
        )


def _payload(
    source: typing.Union[str, SourceTree],
    package: str,
    extras: typing.Sequence[str],
    skip: typing.Sequence[str],
    installed: typing.Sequence[str],
    log: typing.Any,
) -> _Payload:
    from ._closure import collect, metadata_entries, missing_on

    if isinstance(source, SourceTree):
        contents = source.files()
        log.info("tree %s: %d files", source.root, len(contents))
        try:
            declared = source.requirements(extras)
        except BundleError as exc:
            log.debug("no declared requirements: %s", exc)
        else:
            if declared:
                log.info(
                    "the tree declares (not vendored): %s", ", ".join(sorted(declared))
                )
        return _Payload(contents, [], source._import_roots(package))
    distributions = missing_on(installed, source, tuple(extras), tuple(skip))
    log.info("bundling %s", ", ".join(sorted(distributions)) or "nothing")
    return _Payload(collect(distributions), metadata_entries(distributions), [""])


def _write(payload: _Payload, destination: Path, mode: str, package: str, python):
    from ._archive import launcher, shebang, write_tree, write_zipapp

    if mode == "pyz":
        return write_zipapp(
            destination, payload.contents, payload.metadata, package, shebang(python)
        )
    write_tree(destination / "lib", payload.contents, payload.metadata)
    (destination / "bin").mkdir(parents=True, exist_ok=True)
    roots = [f"lib/{root}" if root else "lib" for root in payload.roots]
    (destination / "bin" / package).write_bytes(launcher(package, python, roots))
    return destination


def bundle(
    source: typing.Union[str, SourceTree],
    destination: typing.Union[str, "os.PathLike[str]"],
    *,
    mode: str = "pyz",
    package: typing.Optional[str] = None,
    extras: typing.Sequence[str] = (),
    skip: typing.Sequence[str] = (),
    python: str = "python3",
) -> Path:
    """Write a self-contained bundle of ``source`` at ``destination``.

    ``source`` is an installed distribution's name (its whole dependency
    closure ships, markers evaluated for this interpreter) or a
    :class:`SourceTree`. ``mode="pyz"`` writes an executable zipapp file;
    ``mode="dir"`` writes a directory holding ``lib/`` and the launcher
    ``bin/<package>``. ``package`` is the import name run (default: the
    distribution name with ``-`` as ``_``; required for a tree). ``extras``
    are the distribution's extras to include; ``skip`` names distributions to
    leave out. ``python`` is the interpreter the bundle names. Returns the
    written path. Raises `BundleError` for a distribution with a compiled
    extension or nothing importable, `ValueError` for a bad ``mode`` or
    ``package``.
    """
    _require("deploy", "bundle()")
    _check(source, mode)
    package = _package(source, package)
    payload = _payload(source, package, extras, skip, (), _LOGGER)
    return _write(payload, Path(destination), mode, package, python)


def deploy(
    host: "Host",
    source: typing.Union[str, SourceTree],
    path: str,
    *,
    mode: str = "pyz",
    package: typing.Optional[str] = None,
    extras: typing.Sequence[str] = (),
    skip: typing.Sequence[str] = (),
    force: bool = False,
    python: str = "python3",
) -> Deployment:
    """Install ``source`` at ``path`` on a POSIX ``host`` and say how to run it.

    For a distribution name, ``python`` on the target is asked which
    distributions it has, and only the rest of the closure ships, markers
    evaluated for the target. ``mode="pyz"`` installs one zipapp file at
    ``path``; ``mode="dir"`` a tree at ``path`` with ``bin/<package>``. The
    payload is reproducible: when ``path`` already holds a deploy with the same
    digest nothing is written, unless ``force``. A ``path`` that exists but
    was not written by a deploy is never replaced, ``force`` or not.
    ``package``, ``extras`` and ``skip`` are as for :func:`bundle`; ``skip``
    does not apply to a :class:`SourceTree`.

    Raises `ValueError` for a relative ``path`` or a bad ``mode``/``package``,
    `BundleError` before anything is sent when the bundle cannot be built,
    `NotImplementedError` for a host without POSIX paths, `FileExistsError`
    for a foreign ``path``, and `subprocess.CalledProcessError` when a step on
    the target fails.
    """
    from ._archive import tar_digest
    from ._install import install_pyz, install_tree, probe, require_posix

    _require("deploy", "deploy()")
    _check(source, mode)
    package = _package(source, package)
    if not PurePosixPath(path).is_absolute():
        raise ValueError(f"deploy path must be absolute: {path!r}")
    # A trailing slash would put `<path>.new` inside the target.
    target = path.rstrip("/") or path
    require_posix(host)

    installed: typing.Sequence[str] = ()
    if not isinstance(source, SourceTree):
        installed = probe(host, python)
    payload = _payload(source, package, extras, skip, installed, host.logger)

    with tempfile.TemporaryDirectory(prefix="hostctl-deploy-") as scratch:
        staged = Path(scratch) / PurePosixPath(target).name
        if mode == "pyz":
            data = _write(payload, staged, mode, package, python).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
        else:
            digest, data = tar_digest(_write(payload, staged, mode, package, python))
    if mode == "pyz":
        written = install_pyz(host, data, digest, target, force)
        command: typing.Tuple[str, ...] = (python, target)
    else:
        written = install_tree(host, data, digest, target, force)
        command = (f"{target}/bin/{package}",)
    host.logger.info("%s %s", "deployed" if written else "verified", target)
    return Deployment(target, digest, written, command)
