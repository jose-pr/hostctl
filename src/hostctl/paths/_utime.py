"""Spelling a `_utime()` time for the remote tool that sets it."""

from __future__ import annotations

import decimal
import typing

#: .NET `DateTime` ticks (100 ns) from 0001-01-01 to the Unix epoch.
_DOTNET_EPOCH_TICKS = 621_355_968_000_000_000

_NS_PER_SECOND = 1_000_000_000


def epoch_ns(seconds: float) -> int:
    """`seconds` since the epoch in whole nanoseconds.

    `Decimal` reads the float's exact value; `seconds * 1e9` in floating
    point is off by hundreds of nanoseconds at today's epoch.
    """
    return int(round(decimal.Decimal(seconds) * _NS_PER_SECOND))


def dotnet_ticks(seconds: float) -> int:
    """A UTC `DateTime` tick count for `seconds` since the epoch.

    Truncated, never rounded up: every backend here reports whole seconds,
    and a time rounded into the next second would never match its source.
    """
    return _DOTNET_EPOCH_TICKS + epoch_ns(seconds) // 100


def touch_stamp(seconds: float, *, whole_seconds: bool = False) -> str:
    """`touch -d`'s `@EPOCH` operand: nanoseconds, or floored whole seconds."""
    ns = epoch_ns(seconds)
    if whole_seconds or ns % _NS_PER_SECOND == 0:
        return "@%d" % (ns // _NS_PER_SECOND)
    whole, fraction = divmod(abs(ns), _NS_PER_SECOND)
    return "@%s%d.%09d" % ("-" if ns < 0 else "", whole, fraction)


def touch_steps(
    atime: typing.Optional[float], mtime: typing.Optional[float]
) -> typing.List[typing.Tuple[typing.Optional[str], float]]:
    """The `touch` runs that set these times: `(option, seconds)` each.

    `-d` gives both times one value, so two different times take one run
    with `-a` and one with `-m`; `None` leaves a time alone.
    """
    if atime is None and mtime is None:
        return []
    if atime is None:
        return [("-m", typing.cast(float, mtime))]
    if mtime is None:
        return [("-a", atime)]
    if atime == mtime:
        return [(None, mtime)]
    return [("-a", atime), ("-m", mtime)]


#: Runs one argv on the target: `(returncode, stderr text)`.
TouchRunner = typing.Callable[[typing.Sequence[str]], typing.Tuple[int, str]]


class Touch:
    """Sets an existing entry's times with `touch -c -d @EPOCH`.

    GNU coreutils takes a fraction to the nanosecond; busybox takes whole
    seconds only and calls `@N.F` an invalid date, so that refusal switches
    this instance to whole seconds for good. `-c` never creates a file: the
    caller proves the path exists first.
    """

    def __init__(self, run: TouchRunner) -> None:
        self._run = run
        self.whole_seconds = False

    def utime(
        self,
        path: str,
        *,
        atime: typing.Optional[float],
        mtime: typing.Optional[float],
        error: typing.Callable[[str], OSError],
    ) -> None:
        """Set the times; `error(stderr)` is raised for a refusal."""
        for option, seconds in touch_steps(atime, mtime):
            while True:
                stamp = touch_stamp(seconds, whole_seconds=self.whole_seconds)
                argv = ["touch", "-c"]
                if option is not None:
                    argv.append(option)
                argv += ["-d", stamp, "--", path]
                code, stderr = self._run(argv)
                if code == 0:
                    break
                if (
                    not self.whole_seconds
                    and "." in stamp
                    and "invalid date" in stderr.lower()
                ):
                    self.whole_seconds = True
                    continue
                raise error(stderr)
