"""One command over several hosts: target lists, output records, statuses.

A URI operand may name several hosts: a comma separates them, except inside
the user part, and ``[1-3]``/``[a-c]`` ranges expand (``duho.expand``). A
later host without a scheme of its own takes the previous one's scheme and
user, so ``ssh://admin@nas1,nas2`` is two SSH hosts as ``admin``.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import typing

from duho import Arg, CommandError, Meta

from .._registry import host_schemes
from ..uri._parse import redact_uri
from ._common import FAILED, HostCommand, write
from ._status import describe, status_of, worst

_LOGGER = logging.getLogger(__name__)
_AUTHORITY = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")


def _split_userinfo(text: str) -> typing.Tuple[str, str]:
    """``(scheme://user@, rest)``: everything up to the authority's last ``@``
    is credentials, where no comma or range is target syntax."""
    match = _AUTHORITY.match(text)
    prefix, rest = (match.group(0), text[match.end() :]) if match else ("", text)
    end = next((i for i, char in enumerate(rest) if char in "/?#"), len(rest))
    if "@" not in rest[:end]:
        return prefix, rest
    userinfo, _, host = rest[:end].rpartition("@")
    return prefix + userinfo + "@", host + rest[end:]


def _own_scheme(piece: str, schemes: typing.Collection[str]) -> bool:
    """Whether a later piece names its own scheme rather than a host:port."""
    match = _SCHEME.match(piece)
    if match is None:
        return False
    return bool(_AUTHORITY.match(piece)) or match.group(1).lower() in schemes


def expand_targets(operands: typing.Iterable[str]) -> typing.List[str]:
    """Every target the URI operands name, in order."""
    from duho import expand

    schemes = set(host_schemes())
    targets: typing.List[str] = []
    for operand in operands:
        prefix, rest = _split_userinfo(operand)
        for index, piece in enumerate(part for part in rest.split(",") if part):
            if index and _own_scheme(piece, schemes):
                prefix, piece = _split_userinfo(piece)
            targets.extend(prefix + host for host in expand(piece))
    return targets


def prefixed(label: str, data: typing.Union[bytes, str, None]):
    """Each line of `data` behind ``[label] ``, ending in a newline."""
    if not data:
        return data
    if isinstance(data, bytes):
        tag = f"[{label}] ".encode("utf-8", "backslashreplace")
        text = b"".join(tag + line for line in data.splitlines(keepends=True))
        return text if text.endswith((b"\n", b"\r")) else text + b"\n"
    tag = f"[{label}] "
    text = "".join(tag + line for line in data.splitlines(keepends=True))
    return text if text.endswith(("\n", "\r")) else text + "\n"


Emit = typing.Callable[[str, typing.Any, typing.TextIO, typing.TextIO], int]


class FanOutCommand(HostCommand):
    """A command that runs on every host its URI operands name."""

    uri: str
    "Connection URI (see `hostctl --help`); a comma list or [1-3] range names several hosts"
    ("uri",)

    more: Arg[typing.List[str], Meta(metavar="URI")] = []
    "More connection URIs to run the same command on"
    ("more",)

    parallel: int = 1
    "How many hosts to work on at once when there are several"

    def targets(self) -> typing.List[str]:
        """The hosts named, at least one."""
        targets = expand_targets([self.uri, *self.more])
        if not targets:
            raise CommandError("no host named", FAILED)
        return targets

    def fan_out(
        self,
        targets: typing.Sequence[str],
        work: typing.Callable[[typing.Any], typing.Any],
        emit: Emit,
    ) -> int:
        """Run `work(host)` on every target and `emit` each result as it ends.

        Output records carry the target's label; a target that fails gets a
        ``hostctl: [label] exit status N`` line on stderr. Returns the worst
        status (:func:`worst`).
        """
        from duho.fanout import run_targets

        if self.parallel < 1:
            raise CommandError("--parallel must be at least 1", FAILED)
        # Read once, here, so every host shares one prompt: it asks the first
        # time a host needs a password, and the others reuse the answer.
        credentials = self.credentials()
        out, err = sys.stdout, sys.stderr
        lock = threading.Lock()

        def report(label: str, code: int, message: str = "") -> None:
            line = f"hostctl: [{label}] exit status {code}"
            write(err, f"{line}: {message}\n" if message else line + "\n")

        def one(target: str) -> int:
            label = redact_uri(target)
            try:
                with self.open(target, credentials) as host:
                    value = work(host)
            except Exception as error:  # noqa: BLE001 - one host never stops the rest
                code = status_of(error)
                if code is None:
                    _LOGGER.debug("%s failed", label, exc_info=True)
                    code, message = 1, f"{type(error).__name__}: {error}"
                else:
                    message = describe(str(error), error)
                with lock:
                    report(label, code, message)
                return code
            with lock:
                code = emit(label, value, out, err)
                if code:
                    report(label, code)
            return code

        return run_targets(
            one,
            list(targets),
            max_workers=self.parallel,
            label=redact_uri,
            aggregate=worst,
            logger=_LOGGER,
        )
