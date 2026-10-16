"""What a config accepts for its shell and its path grammar."""

from __future__ import annotations

import typing
from pathlib import PurePath

from pathapi import Pathname

from ..shell import ShellFlavourSelection

#: A pure-path or pathapi `Pathname` class: the grammar a host's paths use.
PathnameType = typing.Type[typing.Union[PurePath, Pathname]]

#: A shell flavour, its class or registered name, or `"auto"` to detect it.
ShellSelection = typing.Union[ShellFlavourSelection, typing.Literal["auto"]]

#: A path grammar class, `"posix"`, `"windows"`, or `"auto"` to detect it.
PathnameSelection = typing.Union[
    PathnameType, typing.Literal["posix", "windows", "auto"]
]
