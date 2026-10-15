"""Deterministic adversarial values for the real-shell corpus.

No random generation and no new dependency: the seeds are shapes that broke a
quoting rule at least once, and `generated()` adds every string of one or two
characters over the atoms that carry meaning for some parser.
"""

from __future__ import annotations

import itertools
import typing

BS = "\\"

#: Control characters raise by design, so none appear here; one separate test
#: asserts every flavour refuses them.
_RAW_SEEDS = [
    # POSIX / fish expansion and quoting shapes
    "",
    "plain",
    "a b",
    "-n",
    "-e",
    "--",
    "'",
    "''",
    "a'b",
    '"',
    'a"b',
    BS,
    BS + BS,
    "a" + BS,
    "a" + BS + "'",
    BS + "'",
    "$HOME",
    "${HOME}",
    "$(id -u)",
    "`id -u`",
    "!",
    "!!",
    "^",
    "%",
    "%self",
    "%1",
    "%last",
    "$fish_pid",
    "(echo injected)",
    "{a,b}",
    "~",
    "~root",
    "*",
    "?",
    "[ab]",
    "#x",
    "a;b",
    "a&b",
    "a|b",
    "<x",
    ">x",
    "=ls",
    "=",
    "a=b",
    "x=$(id)",
    "@x",
    "+x",
    ":x",
    ",x",
    "a~b",
    "^x",
    "<1-10>",
    "café",
    "‘q’",
    "\U0001f600",
    "a  b",
    " lead",
    "trail ",
    "-",
    "&",
    ";",
    "a" + BS + "nb",
    "%s",
    "$",
    "${",
    "$'x'",
    '$"x"',
    # PowerShell 5 quote-before-whitespace shapes
    'report" /MIR',
    'a"b c',
    '"a b"',
    'x" y',
    '" "',
    "a" + BS + '"b c',
    'my file" /MIR "z',
    'q"x',
    'a""b c',
    'x"" y',
    '"""a b',
    "trail sp" + BS,
    'one " two " three',
    'k" v w',
    'a b" c',
    # cmd and batch shapes
    "100%",
    "%USERNAME%",
    "%OS%",
    "c^d",
    "a!b",
    "x&y",
    "(x)",
    'say "hi"',
    "--%",
    'x" /MIR "z',
    "q" + BS + BS + '" x',
    "C:" + BS + "dir with space" + BS,
]

SEEDS: typing.List[str] = list(dict.fromkeys(_RAW_SEEDS))

_ATOMS = ('"', BS, " ", "%", "'", "&", "^", "a")


def generated() -> typing.List[str]:
    """Every string of length 1 and 2 over the atoms that carry syntax."""
    out = [a for a in _ATOMS]
    out.extend("".join(pair) for pair in itertools.product(_ATOMS, repeat=2))
    return out


VALUES: typing.List[str] = list(dict.fromkeys(SEEDS + generated()))

#: Templates whose `{marker}` is a fresh path per value. A marker file existing
#: afterwards means the value ran as a command.
WINDOWS_HOSTILE = (
    'x"&echo INJ>{marker}',
    'x" & echo INJ>{marker} & "',
    'a b" & echo INJ>{marker} & "',
    "a|echo INJ>{marker}",
)
POSIX_HOSTILE = WINDOWS_HOSTILE + (
    "$(touch {marker})",
    "`touch {marker}`",
    ";touch {marker}",
)


def fs_names(os_family: str, values: typing.Iterable[str] = VALUES) -> typing.List[str]:
    """The values a file or directory name can carry on `os_family`."""
    names = []
    for value in values:
        if not value or value in (".", ".."):
            continue
        if "/" in value or "\x00" in value:
            continue
        if os_family == "nt":
            if any(char in value for char in '"<>|?*:\\'):
                continue
            if value.endswith((" ", ".")):
                continue
            if not value.isascii():
                continue
        names.append(value)
    return names
