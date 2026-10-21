"""The ``hostctl`` command line, built on ``duho``.

``main`` is the entry point of the ``hostctl`` console script and of
``python -m hostctl``. It needs the ``cli`` extra (``pip install
hostctl[cli]``); importing this package does not, so a bare install gets one
line naming the extra instead of an ``ImportError``.
"""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from ._root import Hostctl

__all__ = ["main"]


def main(argv: typing.Optional[typing.Sequence[str]] = None) -> int:
    """Run the command line and return its exit status.

    ``run`` returns the remote command's status; the command line's own
    failures are 125 (connection, timeout, usage, unsupported), 126
    (permission denied) and 127 (not found). A parser usage error raises
    :class:`SystemExit` with 2. Ctrl-C ends with 130 and a closed standard
    output with 1, both without a message. Without ``duho`` this raises
    :class:`SystemExit` naming the ``cli`` extra.
    """
    try:
        import duho
    except ImportError as exc:
        from ..exceptions import MissingExtraError

        missing = MissingExtraError("cli", "the hostctl command line")
        raise SystemExit(f"hostctl: {missing}") from exc

    from ._root import Hostctl

    return duho.run(Hostctl, argv, exit="parse")


def __getattr__(name: str) -> typing.Any:
    """``Hostctl``, imported on first use: its base class needs ``duho``."""
    if name == "Hostctl":
        from ._root import Hostctl

        return Hostctl
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
