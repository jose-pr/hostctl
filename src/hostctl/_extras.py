"""Which optional extras are installed, and the error that names a missing one.

One table, so the probes, the install hints and the providers' availability
agree on what each extra needs.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import typing

#: extra -> (modules it must provide, the oldest Python it supports)
_EXTRAS: typing.Dict[
    str, typing.Tuple[typing.Tuple[str, ...], typing.Tuple[int, int]]
] = {
    "ssh": (("asyncssh", "sftppath"), (3, 9)),
    "winrm": (("winrm",), (3, 9)),
    "psrp": (("pypsrp",), (3, 10)),
    "container": (("docker",), (3, 9)),
    "serial": (("serial",), (3, 9)),
    "qemu-libvirt": (("libvirt",), (3, 9)),
}


def _missing(extra: str) -> typing.Tuple[str, ...]:
    """The modules of `extra` that cannot be imported, without importing any."""
    names, _ = _EXTRAS[extra]
    absent = []
    for name in names:
        try:
            found = importlib.util.find_spec(name) is not None
        except ValueError:
            # Already imported with no spec: present.
            found = True
        except ImportError:
            # A finder that refuses the name outright.
            found = False
        if not found:
            absent.append(name)
    return tuple(absent)


def _available(extra: str) -> bool:
    _, floor = _EXTRAS[extra]
    return sys.version_info >= floor and not _missing(extra)


def _require(extra: str, feature: str) -> None:
    """Import what `extra` provides, or raise `ImportError` naming it."""
    names, floor = _EXTRAS[extra]
    if sys.version_info < floor:
        raise ImportError(
            f"{feature} needs Python {floor[0]}.{floor[1]} or newer "
            f"(the '{extra}' extra supports nothing older)"
        )
    for name in names:
        try:
            importlib.import_module(name)
        except ImportError as exc:
            raise ImportError(
                f"{feature} needs the '{extra}' extra: pip install \"hostctl[{extra}]\""
            ) from exc


def has_ssh() -> bool:
    """Whether the `ssh` extra is installed: SSH commands and SFTP paths.

    Imports nothing; `True` means both asyncssh and sftppath are importable.
    """
    return _available("ssh")


def has_psrp() -> bool:
    """Whether the `psrp` extra is installed and this Python can use it (3.10+).

    Imports nothing.
    """
    return _available("psrp")
