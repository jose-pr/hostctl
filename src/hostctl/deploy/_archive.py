"""The two layouts a bundle is written in, each reproducible byte for byte.

A zipapp is one file CPython runs directly; a tree is the same file set
unpacked under ``lib/`` with a launcher in ``bin/``. Both stamp every entry
with one fixed time and mode, so a rebuild of the same inputs has the same
digest and a deploy can skip an unchanged target.
"""

from __future__ import annotations

import hashlib
import io
import shlex
import shutil
import tarfile
import typing
import zipfile
from pathlib import Path

#: 1980-01-01, the earliest time a zip entry can carry; the tar uses the same.
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
EPOCH = 315532800

#: The zipapp entry point: `-m` semantics, so the package's own `__main__`
#: decides what running it means.
_MAIN_TEMPLATE = """\
import runpy
import sys

sys.argv[0] = {package!r}
runpy.run_module({package!r}, run_name="__main__", alter_sys=True)
"""

#: `bin/<package>` of a tree: the interpreter is chosen on the target at run
#: time, with the tree's import roots ahead of the inherited PYTHONPATH.
_LAUNCHER_TEMPLATE = """\
#!/bin/sh
here=$(cd "$(dirname "$0")/.." && pwd)
PYTHONPATH="{pythonpath}${{PYTHONPATH:+:$PYTHONPATH}}" exec {python} -m {package} "$@"
"""

Contents = typing.Sequence[typing.Tuple[str, Path]]
Metadata = typing.Sequence[typing.Tuple[str, bytes]]


def _zipwrite(archive: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    info = zipfile.ZipInfo(arcname, date_time=_ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def shebang(python: str) -> str:
    """The ``#!`` line that runs ``python``: as given when absolute, else
    through ``/usr/bin/env``."""
    return f"#!{python}" if python.startswith("/") else f"#!/usr/bin/env {python}"


def write_zipapp(
    destination: Path,
    contents: Contents,
    metadata: Metadata,
    package: str,
    interpreter: typing.Optional[str],
) -> Path:
    """Write a zipapp running ``package``; ``interpreter`` is its ``#!`` line
    (``None`` for none). Entries are sorted and stamped with one fixed time."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname, source in sorted(contents):
            _zipwrite(archive, arcname, Path(source).read_bytes())
        for arcname, blob in metadata:
            _zipwrite(archive, arcname, blob)
        _zipwrite(
            archive, "__main__.py", _MAIN_TEMPLATE.format(package=package).encode()
        )
    # The sources come from several distributions, not one directory, which
    # is the only shape `zipapp.create_archive` takes; the shebang is ours.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "wb") as handle:
        if interpreter:
            handle.write(f"{interpreter}\n".encode())
        handle.write(buffer.getvalue())
    destination.chmod(0o755)
    return destination


def write_tree(destination: Path, contents: Contents, metadata: Metadata) -> Path:
    """Copy ``contents`` and ``metadata`` under ``destination``."""
    destination.mkdir(parents=True, exist_ok=True)
    for arcname, source in contents:
        target = destination / arcname
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for arcname, blob in metadata:
        target = destination / arcname
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    return destination


def launcher(package: str, python: str, roots: typing.Sequence[str]) -> bytes:
    """The ``bin/<package>`` script; ``roots`` are import roots relative to the
    tree's top (``lib``, ``lib/repo/src``). Bytes, so no CRLF reaches the
    target from a Windows build."""
    pythonpath = ":".join(f"$here/{root}" for root in roots)
    text = _LAUNCHER_TEMPLATE.format(
        pythonpath=pythonpath, python=shlex.quote(python), package=package
    )
    return text.encode()


def tar_tree(path: Path, executable_dirs: typing.Sequence[str] = ("bin",)) -> bytes:
    """Tar a directory in memory, owned by 0:0 with deterministic modes.

    Directories and files directly under ``executable_dirs`` are 0755, other
    files 0644, so no build machine's ids or umask reach the target and a
    launcher arrives runnable. Caches are left out.
    """
    executable = {directory.strip("/") for directory in executable_dirs}
    buffer = io.BytesIO()

    def normalize(info: tarfile.TarInfo) -> typing.Optional[tarfile.TarInfo]:
        relative = info.name[len(path.name) :].lstrip("/")
        parts = relative.split("/") if relative else []
        if "__pycache__" in parts or relative.endswith((".pyc", ".pyo")):
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        info.mtime = EPOCH
        if info.isdir():
            info.mode = 0o755
        elif len(parts) == 2 and parts[0] in executable:
            info.mode = 0o755
        else:
            info.mode = 0o644
        return info

    with tarfile.open(fileobj=buffer, mode="w") as tar:
        tar.add(path.as_posix(), path.name, filter=normalize)
    return buffer.getvalue()


def tar_digest(
    path: Path, executable_dirs: typing.Sequence[str] = ("bin",)
) -> typing.Tuple[str, bytes]:
    """``(sha256 hex, tar bytes)`` of :func:`tar_tree`."""
    content = tar_tree(path, executable_dirs)
    return hashlib.sha256(content).hexdigest(), content
