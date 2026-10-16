"""How a system host renders a command for one provider."""

from __future__ import annotations


import typing


from ..shell._invocation import shell_invocation


from ..executor import PathLike


class _ShellContext(typing.NamedTuple):
    """What the SHELL layer must supply, because the provider cannot.

    `cwd` and `env` are `None` when the provider carries them natively --
    passing them to the flavour as well would apply them twice. Computed
    once per dispatch: the combinations are the bug surface, so there is one
    expression of them.
    """

    cwd: typing.Optional[PathLike]
    env: typing.Optional[typing.Any]
    epilogue: bool


def _shell_invocation(flavour, cmds, *, cwd, env, epilogue, executable, provider=None):
    """`shell_invocation` for the provider's declared login shell."""
    return shell_invocation(
        flavour,
        cmds,
        cwd=cwd,
        env=env,
        epilogue=epilogue,
        executable=executable,
        login_shell=getattr(provider, "login_shell", None),
    )
