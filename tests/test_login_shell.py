"""A command line survives the login shell that receives it.

AsyncSSH sends one command STRING, and the server hands it to a login
shell. On POSIX that is `$SHELL -c`, which is exactly what this package
renders for -- one parse, nothing to add. Windows OpenSSH's stock server
hands it to `cmd.exe /c`, so a `cmd`-dialect command line (which already
carries one cmd layer) is parsed by cmd TWICE.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from pathlib_next import PosixPathname, WindowsPathname

from hostctl import SshConfig
from hostctl.shell import CMD, POSIX_SHELL, POWERSHELL

#: The values that separate a working rule from a plausible one. Measured
#: against a real cmd.exe standing in for the stock server: unescaped, 4 of
#: these 8 survive -- `%OS%` expands to `Windows_NT`, `c^d` arrives as `cd`,
#: and `a&b` and `x|y` INJECT.
ADVERSARIAL = ("a b", "%OS%", "a&b", 'say "hi"', "c^d", "(x)", "100%", "x|y")


def test_auto_reads_the_dialect():
    assert SshConfig("h").resolved_login_shell == "posix"
    assert SshConfig("h", path_flavor=WindowsPathname).resolved_login_shell == "cmd"
    # Explicit wins: the server's DefaultShell is configurable, so guessing
    # is wrong in both directions.
    assert (
        SshConfig(
            "h", path_flavor=WindowsPathname, login_shell="powershell"
        ).resolved_login_shell
        == "powershell"
    )
    assert SshConfig("h", login_shell="posix").resolved_login_shell == "posix"


def test_an_unknown_login_shell_is_refused():
    with pytest.raises(ValueError, match="login_shell"):
        SshConfig("h", login_shell="fish")


def test_only_a_flavour_with_an_outer_rule_claims_one():
    """The default refuses rather than guessing.

    A POSIX-rendered line delivered to `sh -c` is the ordinary single-parse
    case and never needs this; a flavour without a rule must not pretend.
    """
    # A line with nothing cmd would consume needs no escaping; one with
    # metacharacters does.
    assert CMD.escape_for_one_parse("echo ok") == "echo ok"
    assert CMD.escape_for_one_parse("a&b") == "a^&b"
    for flavour in (POSIX_SHELL, POWERSHELL):
        with pytest.raises(NotImplementedError):
            flavour.escape_for_one_parse("echo ok")


@pytest.mark.skipif(sys.platform != "win32", reason="requires a real cmd.exe")
@pytest.mark.parametrize("value", ADVERSARIAL)
def test_a_cmd_line_survives_a_second_cmd_parse(value):
    """What a stock Windows OpenSSH server does to the string it receives."""
    code = "import sys; sys.stdout.write(ascii(sys.argv[1]))"
    inner = CMD.command(((sys.executable, "-c", code, value),)).command

    delivered = CMD.escape_for_one_parse(inner)
    completed = subprocess.run(
        f"cmd.exe /d /s /c {delivered}",
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr[:300]
    assert completed.stdout.strip() == ascii(value)


@pytest.mark.skipif(sys.platform != "win32", reason="requires a real cmd.exe")
@pytest.mark.parametrize("value", ("a&b", "x|y", "%OS%", "c^d"))
def test_the_unescaped_line_is_what_this_protects_against(value):
    """The control, kept in the suite: these four fail without the layer.

    Two of them are command injection, not corruption -- `a&b` ends the
    command and starts another.
    """
    code = "import sys; sys.stdout.write(ascii(sys.argv[1]))"
    inner = CMD.command(((sys.executable, "-c", code, value),)).command

    completed = subprocess.run(
        f"cmd.exe /d /s /c {inner}",
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.stdout.strip() != ascii(value), (
        f"{value!r} survived an unescaped double parse; if the platform "
        "changed, the escaping rule needs re-measuring"
    )


def test_a_posix_target_adds_no_layer():
    """One parse, and the POSIX rendering already targets it."""
    from hostctl.host.system import _for_login_shell

    class _Provider:
        login_shell = "posix"

    line = POSIX_SHELL.script((("tool", "a b"),))
    assert _for_login_shell(POSIX_SHELL, line, _Provider()) == line


def test_a_windows_target_adds_the_layer():
    from hostctl.host.system import _for_login_shell

    class _Provider:
        login_shell = "cmd"

    line = CMD.command((("tool.exe", "a&b"),)).command
    assert _for_login_shell(CMD, line, _Provider()) == CMD.escape_for_one_parse(line)


def test_a_provider_that_says_nothing_changes_nothing():
    """`CreateProcess` does not parse, so the local path must be untouched."""
    from hostctl.host.system import _for_login_shell

    line = CMD.command((("tool.exe", "a&b"),)).command
    assert _for_login_shell(CMD, line, object()) == line


def test_the_uri_carries_the_setting():
    from hostctl.host import HostConfig

    config = SshConfig("h", path_flavor=WindowsPathname, login_shell="powershell")
    restored = HostConfig(config.connection_uri)
    assert isinstance(restored, SshConfig)
    assert restored.login_shell == "powershell"
    assert restored.path_flavor is WindowsPathname

    # `auto` is the default and is left out, so an existing URI's text is
    # unchanged by this field existing.
    default = SshConfig("h", path_flavor=PosixPathname)
    assert "login_shell" not in default.connection_uri
    assert HostConfig(default.connection_uri).login_shell == "auto"


def test_the_constructor_takes_a_host_and_the_dispatcher_takes_a_uri():
    """Passed a URI, the constructor made it the HOSTNAME.

    The same trap `SerialConfig` has: two entry points whose first argument
    is a different kind of thing. The failure used to surface much later, as
    an unresolvable name.
    """
    with pytest.raises(ValueError, match="takes a host, not a URI"):
        SshConfig("ssh://root@h:22")
