"""Public shell construction API and built-in shell flavours."""

import typing
from importlib import metadata as _metadata

from ._shell import Shell as Shell, ShellSession as ShellSession
from ._vocabulary import (
    ShellCommand as ShellCommand,
    ShellOperator as ShellOperator,
    ShellTarget as ShellTarget,
    ShellToken as ShellToken,
    wants_epilogue as wants_epilogue,
)
from ._flavour import ShellFlavour as ShellFlavour
from ._grammar import Cmdlet as Cmdlet, Exec as Exec, PSParam as PSParam
from ._cmd import CmdShellFlavour as CmdShellFlavour
from ._fish import FishShellFlavour as FishShellFlavour
from ._posix import (
    BashShellFlavour as BashShellFlavour,
    PosixShellFlavour as PosixShellFlavour,
    ZshShellFlavour as ZshShellFlavour,
)
from ._powershell import PowerShellFlavour as PowerShellFlavour

POSIX_SHELL = PosixShellFlavour()
BASH = BashShellFlavour()
ZSH = ZshShellFlavour()
FISH = FishShellFlavour()
CMD = CmdShellFlavour()
POWERSHELL = PowerShellFlavour()
PWSH = PowerShellFlavour(major_version=7)

_SHELLS = {
    POSIX_SHELL.name: POSIX_SHELL,
    BASH.name: BASH,
    ZSH.name: ZSH,
    FISH.name: FISH,
    CMD.name: CMD,
    POWERSHELL.name: POWERSHELL,
    PWSH.name: PWSH,
}

ShellFlavourSelection = typing.Union[str, ShellFlavour, typing.Type[ShellFlavour]]


def register_shell_flavour(
    value: typing.Union[ShellFlavour, typing.Type[ShellFlavour]],
    *,
    name: typing.Optional[str] = None,
    replace: bool = False,
) -> ShellFlavour:
    """Register a configured flavour instance for string-based selection."""
    flavour = value() if isinstance(value, type) else value
    if not isinstance(flavour, ShellFlavour):
        raise TypeError("shell flavour must be a ShellFlavour instance or subclass")
    selected_name = name or flavour.name
    if not selected_name:
        raise ValueError("shell flavour name must not be empty")
    if selected_name in _SHELLS and not replace:
        raise ValueError(f"shell flavour is already registered: {selected_name}")
    _SHELLS[selected_name] = flavour
    return flavour


def shell_flavour_names() -> typing.Tuple[str, ...]:
    """Sorted names `shell_flavour()` accepts: registered and plugin flavours."""
    names = set(_SHELLS)
    try:
        entries = _metadata.entry_points()
        selected = (
            entries.select(group="hostctl.shell_flavours")
            if hasattr(entries, "select")
            else entries.get("hostctl.shell_flavours", ())
        )
        names.update(item.name for item in selected)
    except Exception:  # a broken plugin index must not hide the built-ins
        pass
    return tuple(sorted(names))


def shell_flavour(value: ShellFlavourSelection) -> ShellFlavour:
    """Normalize a public shell selection without inferring from the transport."""
    if isinstance(value, type):
        if not issubclass(value, ShellFlavour):
            raise TypeError("shell flavour class must inherit ShellFlavour")
        value = value()
    if isinstance(value, ShellFlavour):
        return value
    try:
        return _SHELLS[value]
    except (KeyError, TypeError) as exc:
        if isinstance(value, str):
            try:
                entries = _metadata.entry_points()
                selected = (
                    entries.select(group="hostctl.shell_flavours", name=value)
                    if hasattr(entries, "select")
                    else [
                        item
                        for item in entries.get("hostctl.shell_flavours", ())
                        if item.name == value
                    ]
                )
                if selected:
                    loaded = selected[0].load()
                    register_shell_flavour(loaded, name=value)
                    return _SHELLS[value]
            except Exception as plugin_error:
                raise ValueError(
                    f"invalid shell flavour plugin {value!r}: {plugin_error}"
                ) from plugin_error
        supported = ", ".join(sorted(_SHELLS))
        raise ValueError(
            f"unsupported shell flavour; choose one of: {supported}"
        ) from exc


__all__ = [
    "POSIX_SHELL",
    "BASH",
    "ZSH",
    "FISH",
    "CMD",
    "Cmdlet",
    "Exec",
    "BashShellFlavour",
    "CmdShellFlavour",
    "FishShellFlavour",
    "POWERSHELL",
    "PosixShellFlavour",
    "PowerShellFlavour",
    "PSParam",
    "PWSH",
    "Shell",
    "ShellCommand",
    "ShellFlavour",
    "ShellFlavourSelection",
    "ShellOperator",
    "ShellTarget",
    "ShellSession",
    "ShellToken",
    "ZshShellFlavour",
    "register_shell_flavour",
    "wants_epilogue",
    "shell_flavour",
    "shell_flavour_names",
]
