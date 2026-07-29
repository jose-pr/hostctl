"""`Exec` marks direct execution explicitly, rather than by position."""

from __future__ import annotations

import subprocess
import sys
from pathlib import PurePosixPath

import pytest
from pathlib_next import PosixPathname

from hostctl import Exec, LocalHost, POSIX_SHELL, POWERSHELL
from hostctl.executor._common import command_text
from hostctl.host._common import starts_direct_command


def test_exec_accepts_str_bytes_and_path_programs():
    # Everything reaching a transport is text, so how the caller held the
    # value is not meant to change what runs.
    for program in (
        "/bin/ls",
        b"/bin/ls",
        PurePosixPath("/bin/ls"),
        PosixPathname("/bin/ls"),
    ):
        command = Exec(program, "-l")
        assert command.program == program
        assert command.args == ("-l",)


def test_exec_is_not_iterable_so_a_shell_cannot_quote_it_as_argv():
    # `ShellFlavour.command_text` dispatches structured commands on Iterable.
    # An iterable marker would be silently rendered into a quoted argv,
    # reintroducing the shell layer the caller asked to skip.
    import collections.abc

    assert not isinstance(Exec("ls"), collections.abc.Iterable)


def test_exec_reaching_a_shell_script_raises_rather_than_being_rendered():
    with pytest.raises(TypeError, match="executed directly"):
        POSIX_SHELL.script([Exec("ls", "-l")])


def test_exec_rejects_nested_commands_and_non_scalar_values():
    with pytest.raises(TypeError, match="scalar values"):
        Exec("ls", ["nested"])
    with pytest.raises(TypeError, match="program must be"):
        Exec(["ls"])
    with pytest.raises(TypeError, match="program must be"):
        Exec(42)


def test_exec_dispatches_as_one_direct_command():
    assert starts_direct_command([Exec("/bin/ls", "-l")]) == ("/bin/ls", ("-l",))


def test_exec_cannot_be_combined_with_other_commands():
    # There is no shell to join them with, and running only one would
    # silently drop the rest.
    with pytest.raises(TypeError, match="cannot be combined"):
        starts_direct_command([Exec("/bin/a"), Exec("/bin/b")])
    with pytest.raises(TypeError, match="cannot be combined"):
        starts_direct_command([Exec("/bin/a"), "echo hi"])


def test_a_bare_path_is_an_ordinary_value_not_an_executable():
    # The change this makes possible: several paths are several commands,
    # because no position is special any more.
    assert (
        starts_direct_command([PurePosixPath("/bin/a"), PurePosixPath("/bin/b")])
        is None
    )
    assert (
        POSIX_SHELL.script([PurePosixPath("/bin/a"), PurePosixPath("/bin/b")])
        == "/bin/a;/bin/b"
    )


def test_a_path_inside_a_structured_command_stays_a_quoted_argument():
    assert (
        POSIX_SHELL.script([["chmod", "755", PurePosixPath("/srv/a b")]])
        == "chmod 755 '/srv/a b'"
    )


def test_local_exec_runs_the_program_without_a_shell():
    result = LocalHost().run(
        Exec(sys.executable, "-c", "import sys; print(sys.argv[1])", "a & b"),
        text=True,
    )

    # `a & b` survives verbatim: no shell interpreted the `&`.
    assert result.stdout.strip() == "a & b"


def test_local_exec_resolves_a_bare_program_name_through_path():
    # Previously unspellable: a bare string is always shell text, so direct
    # execution required knowing an absolute path.
    name = "cmd.exe" if sys.platform == "win32" else "echo"
    args = ("/c", "echo", "hi") if sys.platform == "win32" else ("hi",)

    result = LocalHost().run(Exec(name, *args), text=True)

    assert result.stdout.strip() == "hi"


def test_local_exec_reports_status_like_subprocess():
    failed = LocalHost().run(
        Exec(sys.executable, "-c", "raise SystemExit(3)"), check=False
    )
    assert failed.returncode == 3

    with pytest.raises(subprocess.CalledProcessError):
        LocalHost().run(Exec(sys.executable, "-c", "raise SystemExit(4)"))


class _Duck:
    """A path that never registered with `os.PathLike`."""

    def __fspath__(self):
        return "/real/path"

    def __str__(self):
        return "<Duck object>"


class _BadPath:
    def __fspath__(self):
        raise RuntimeError("boom")

    def __str__(self):
        return "/fallback"


class _NonStrPath:
    def __fspath__(self):
        return 42

    def __str__(self):
        return "/fallback"


def test_command_text_prefers_fspath_over_str():
    # `str()` matches `__fspath__` for the path types shipped here, but that
    # is a coincidence of their `__str__`, not a contract. A transport needs
    # the filesystem representation.
    assert command_text(_Duck()) == "/real/path"
    assert command_text(PurePosixPath("/a b")) == "/a b"
    assert command_text(b"/a b") == "/a b"
    assert command_text(42) == "42"


def test_command_text_falls_back_to_str_for_an_unusable_fspath():
    # A broken `__fspath__` should not fail the whole command.
    assert command_text(_BadPath()) == "/fallback"
    assert command_text(_NonStrPath()) == "/fallback"


def test_shell_values_also_prefer_fspath():
    assert POSIX_SHELL.script([["echo", _Duck()]]) == "echo /real/path"


def test_powershell_structured_command_still_renders_through_the_call_operator():
    # `Exec` changes direct execution only; structured commands are untouched.
    assert (
        POWERSHELL.script([["Get-Item", "C:/a b"]])
        == "& 'Get-Item' 'C:/a b'; exit $LASTEXITCODE"
    )
