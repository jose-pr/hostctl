"""The steps run on the target: probe, compare digests, write, swap.

Everything goes through ``host.run(Exec(...))`` and ``host.path()``, so any
POSIX host works whatever its transport. Each command is an argv with ``--``
before its path operands: no shell parses a path, and none can read as an
option.
"""

from __future__ import annotations

import typing
from pathlib import PurePosixPath

from ..shell._grammar import Exec
from ._closure import PROBE_SOURCE

if typing.TYPE_CHECKING:
    from ..host._host import Host

#: The digest file inside an installed tree.
DIGEST_NAME = ".digest"


def require_posix(host: "Host") -> None:
    """Raise `NotImplementedError` unless the target has POSIX path syntax.

    The install steps are `mv`, `rm`, `mkdir`, `chmod` and `tar`, and the
    launcher is a `/bin/sh` script.
    """
    flavour = getattr(host, "shell_flavour", None)
    pathname = getattr(flavour, "pathname", None)
    if not (isinstance(pathname, type) and issubclass(pathname, PurePosixPath)):
        raise NotImplementedError(
            f"deploy needs a POSIX target; {host.name} uses {flavour!r}"
        )


def probe(host: "Host", python: str) -> typing.List[str]:
    """What :data:`PROBE_SOURCE` prints under ``python`` on the target, by line."""
    host.logger.info("probing installed distributions with %s", python)
    result = host.run(
        Exec(python, "-"), input=PROBE_SOURCE, encoding="utf-8", check=True
    )
    return result.stdout.splitlines()


def _step(host: "Host", program: str, *args: str) -> None:
    host.run(Exec(program, *args), check=True)


def _exists(host: "Host", path: str) -> bool:
    return host.path(path).exists()


def current_digest(host: "Host", marker: str) -> typing.Optional[str]:
    """The digest a previous deploy recorded, or ``None``.

    An unreadable marker means "no usable previous deploy": the remedy is to
    deploy again, so it never fails the call.
    """
    try:
        path = host.path(marker)
        if not path.exists():
            return None
        return path.read_text().strip() or None
    except (OSError, ValueError):
        return None


def refuse_foreign(host: "Host", target: str, marker: str) -> None:
    """Raise `FileExistsError` when ``target`` exists without ``marker``.

    Replacing deletes what is there, and the path is free text: a typo or a
    directory that merely has the right name must not be removed. ``force``
    does not override this.
    """
    if _exists(host, target) and not _exists(host, marker):
        raise FileExistsError(
            f"refusing to replace {target}: it exists but is not a previous "
            f"deploy (no {PurePosixPath(marker).name}); remove it or choose "
            "another path"
        )


def ensure_parent(host: "Host", target: str) -> None:
    """Create ``target``'s parent when missing.

    Checked first: an SFTP server reports an existing directory as a bare
    `OSError("Failure")`, which `exist_ok` does not recognise.
    """
    parent = host.path(str(PurePosixPath(target).parent))
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)


def install_pyz(
    host: "Host", payload: bytes, digest: str, target: str, force: bool
) -> bool:
    """Install one zipapp at ``target``; return whether anything was written.

    The file is written beside the target and renamed over it, so a process
    already running the old one never reads a half-written file.
    """
    marker = f"{target}.digest"
    if not force and current_digest(host, marker) == digest:
        host.logger.info("%s is current (%s)", target, digest[:12])
        return False
    refuse_foreign(host, target, marker)
    ensure_parent(host, target)
    host.logger.info("writing %s (%d bytes)", target, len(payload))
    incoming = f"{target}.new"
    host.path(incoming).write_bytes(payload)
    _step(host, "chmod", "755", "--", incoming)
    _step(host, "mv", "-f", "--", incoming, target)
    host.path(marker).write_bytes(f"{digest}\n".encode())
    return True


def install_tree(
    host: "Host", payload: bytes, digest: str, target: str, force: bool
) -> bool:
    """Install a tree from a tar ``payload``; return whether anything was written.

    The tree is unpacked beside the target, carries its marker before it is
    swapped in, and replaces the old one by two renames. A deploy interrupted
    between those renames leaves the last good tree at ``<target>.old``; it is
    put back first, since the steps below delete ``.old``.
    """
    marker = f"{target}/{DIGEST_NAME}"
    incoming, previous = f"{target}.new", f"{target}.old"
    staging = f"{target}.incoming.tar"

    if not _exists(host, target) and _exists(host, f"{previous}/{DIGEST_NAME}"):
        host.logger.warning("restoring %s from an interrupted deploy", target)
        _step(host, "mv", "--", previous, target)

    if not force and current_digest(host, marker) == digest:
        host.logger.info("%s is current (%s)", target, digest[:12])
        return False
    refuse_foreign(host, target, marker)

    ensure_parent(host, target)
    host.logger.info("uploading %d bytes to %s", len(payload), staging)
    host.path(staging).write_bytes(payload)
    _step(host, "rm", "-rf", "--", incoming)
    _step(host, "mkdir", "-p", "--", incoming)
    _step(host, "tar", "-xf", staging, "-C", incoming, "--strip-components=1")
    host.path(f"{incoming}/{DIGEST_NAME}").write_bytes(f"{digest}\n".encode())
    if _exists(host, target):
        _step(host, "rm", "-rf", "--", previous)
        _step(host, "mv", "--", target, previous)
    _step(host, "mv", "--", incoming, target)
    _step(host, "rm", "-rf", "--", previous, staging)
    return True
