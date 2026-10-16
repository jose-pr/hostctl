"""A command line survives the login shell that receives it.

AsyncSSH sends one command STRING, and the server hands it to a login
shell. On POSIX that is `$SHELL -c`, which is exactly what this package
renders for -- one parse, nothing to add. Windows OpenSSH's stock server
hands it to `cmd.exe /c`, so a `cmd`-dialect command line (which already
carries one cmd layer) is parsed by cmd TWICE.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest
from pathapi import PosixPathname, WindowsPathname

from hostctl.transports.ssh._config import SSHConfig
from hostctl.shell import CMD, FISH, POSIX_SHELL, POWERSHELL

#: The values that separate a working rule from a plausible one. Measured
#: against a real cmd.exe standing in for the stock server: unescaped, 4 of
#: these 8 survive -- `%OS%` expands to `Windows_NT`, `c^d` arrives as `cd`,
#: and `a&b` and `x|y` INJECT.
ADVERSARIAL = ("a b", "%OS%", "a&b", 'say "hi"', "c^d", "(x)", "100%", "x|y")


def test_auto_reads_the_dialect():
    assert SSHConfig("h").resolved_login_shell == "posix"
    assert SSHConfig("h", path_flavor=WindowsPathname).resolved_login_shell == "cmd"
    # Explicit wins: the server's DefaultShell is configurable, so guessing
    # is wrong in both directions.
    assert (
        SSHConfig(
            "h", path_flavor=WindowsPathname, login_shell="powershell"
        ).resolved_login_shell
        == "powershell"
    )
    assert SSHConfig("h", login_shell="posix").resolved_login_shell == "posix"


def test_an_unknown_login_shell_is_refused():
    with pytest.raises(ValueError, match="login_shell"):
        SSHConfig("h", login_shell="fish")


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


@pytest.mark.skipif(shutil.which("cmd.exe") is None, reason="requires cmd.exe")
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


@pytest.mark.skipif(shutil.which("cmd.exe") is None, reason="requires cmd.exe")
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
    """One parse: the rendering already targets the login shell."""
    plain = POSIX_SHELL.command((("tool", "a b"),)).command
    assert POSIX_SHELL.command((("tool", "a b"),), login_shell="posix").command == plain


def test_a_windows_target_adds_the_layer():
    line = CMD.command((("tool.exe", "a&b"),)).command
    delivered = CMD.command((("tool.exe", "a&b"),), login_shell="cmd").command
    assert delivered == CMD.escape_for_one_parse(line)


def test_a_provider_that_says_nothing_changes_nothing():
    """`CreateProcess` does not parse, so the local path must be untouched."""
    line = CMD.command((("tool.exe", "a&b"),)).command
    assert CMD.command((("tool.exe", "a&b"),), login_shell=None).command == line
    # A login shell that is not cmd does not parse a cmd line either.
    assert CMD.command((("tool.exe", "a&b"),), login_shell="posix").command == line


class _Process:
    returncode = None


class _Recording:
    def __init__(self):
        self.commands = []

    def is_closed(self):
        return False

    async def create_process(self, command, **options):
        self.commands.append(command)
        return _Process()


def _transport(**config):
    from hostctl import _async
    from hostctl.transports.ssh._transport import _SSHTransport

    transport = _SSHTransport(SSHConfig("h", **config))
    transport._ssh = _Recording()
    transport._loop_generation = _async.loop_generation()
    return transport


def test_spawn_delivers_the_same_line_as_run():
    transport = _transport(dialect=CMD, path_flavor=WindowsPathname)

    transport.spawn(("tool.exe", "a&b"))

    expected = CMD.command((("tool.exe", "a&b"),), login_shell="cmd").command
    assert transport._ssh.commands == [expected]
    assert expected != CMD.command((("tool.exe", "a&b"),)).command


# Values that separate a working outer rule from a plausible one; the fish
# and sh-family readings of every one of them were measured to agree.
POSIX_VALUES = (
    "a b",
    "x\\",
    "; touch MARKER; #",
    "it's",
    "a\\'b",
    "\\\\",
    "$HOME",
    "$(id)",
    "`id`",
    "%self",
    "%",
    "~",
    "*",
    "{a,b}",
    "",
    '"',
    "(x)",
    "a;b",
    "#c",
    "!x",
    "^x",
    "x\\'",
    "'",
    "''",
    "\\",
    "a=b",
    "=x",
    "@x",
    "-c",
    "%HOME%",
    "$",
    "\\n",
    "end",
    "begin",
    "a&&b",
    "a|b",
    "x\\; touch MARKER; #",
)

_PRINT_ARGV = "import json, sys; sys.stdout.write(json.dumps(sys.argv[1:]))"


def _shell_command(name):
    if name == "busybox ash":
        if shutil.which("busybox") is None:
            return None
        return [shutil.which("busybox"), "ash"]
    found = shutil.which(name)
    return [found] if found else None


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX host")
@pytest.mark.parametrize("dialect", ("posix", "fish"))
@pytest.mark.parametrize("login", ("sh", "bash", "zsh", "fish", "busybox ash"))
def test_a_line_survives_every_posix_family_login_shell(login, dialect, tmp_path):
    shell = _shell_command(login)
    if shell is None:
        pytest.skip(f"{login} is not installed")
    flavour = FISH if dialect == "fish" else POSIX_SHELL
    inner = shutil.which("fish" if dialect == "fish" else "sh")
    if inner is None:
        pytest.skip(f"{dialect} is not installed")
    marker = tmp_path / "MARKER"
    values = tuple(v.replace("MARKER", str(marker)) for v in POSIX_VALUES)

    line = flavour.command(
        ((sys.executable, "-c", _PRINT_ARGV, *values),),
        executable=inner,
        login_shell="posix",
    ).command
    completed = subprocess.run(
        [*shell, "-c", line], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr[:300]
    assert json.loads(completed.stdout) == list(values)
    assert not marker.exists()


def test_the_portable_join_quotes_what_fish_and_sh_read_differently():
    from hostctl.shell._vocabulary import login_join

    assert login_join(["a", "b c"]) == "a 'b c'"
    assert login_join(["x\\"]) == "'x'\\\\"
    assert login_join(["it's"]) == "'it'\\'" + "'s'"
    assert login_join([""]) == "''"
    assert login_join(["%self"]) == "'%self'"
    assert login_join(["=x"]) == "'=x'"


def test_the_uri_carries_the_setting():
    from hostctl.host._config import HostConfig

    config = SSHConfig("h", path_flavor=WindowsPathname, login_shell="powershell")
    restored = HostConfig(config.connection_uri)
    assert isinstance(restored, SSHConfig)
    assert restored.login_shell == "powershell"
    assert restored.path_flavor is WindowsPathname

    # `auto` is the default and is left out, so an existing URI's text is
    # unchanged by this field existing.
    default = SSHConfig("h", path_flavor=PosixPathname)
    assert "login_shell" not in default.connection_uri
    assert HostConfig(default.connection_uri).login_shell == "auto"


def test_the_constructor_takes_a_host_and_the_dispatcher_takes_a_uri():
    """Passed a URI, the constructor made it the HOSTNAME.

    The same trap `SerialConfig` has: two entry points whose first argument
    is a different kind of thing. Accepted, the failure would surface much
    later, as an unresolvable name.
    """
    with pytest.raises(ValueError, match="takes a host, not a URI"):
        SSHConfig("ssh://root@h:22")
