"""Rendering rules that only show up against a real parser."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from hostctl.executor import ExecutorCapability, normalize_input
from hostctl.shell import CMD, POSIX_SHELL, PWSH, ZSH, POWERSHELL
from hostctl.shell._grammar import Cmdlet, PSParam
from hostctl.shell._powershell import PowerShellFlavour
from hostctl.shell._flavour import Shell, ShellOperator, ShellSession, ShellTarget


class _Recorder:
    """An executor that records what it was handed.

    It declares the native `env` capability, because a shell with no such
    executor embeds the assignments in the script instead of forwarding
    them -- which is the path where an empty mapping is harmless.
    """

    executor_capabilities = frozenset({ExecutorCapability.ENV})

    def __init__(self):
        self.calls = []

    def __call__(self, command, *args, env=None, **options):
        options["env"] = env
        self.calls.append((command, args, options))
        return subprocess.CompletedProcess([command, *args], 0, "", "")


class _Process:
    """A process that records what a session writes into it."""

    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    returncode = None


@pytest.mark.parametrize(
    ("script", "expected"),
    (
        ("echo hi", "echo hi\n"),
        ("echo hi;", "echo hi;\n"),
        ("echo hi ;   ", "echo hi ;   \n"),
        ("sleep 5 &", "sleep 5 &\n"),
        ("a | b", "a | b\n"),
        ("a |", "a |\n"),
        ("a &&", "a &&\n"),
    ),
)
def test_send_appends_a_newline_and_no_separator(script, expected):
    """`send()` appended `;` unconditionally, which broke ordinary shell text
    that already ended in a terminator (`echo hi;;`, `sleep 5 &;`), a heredoc
    delimiter, and an open pipeline. A newline ends a command in every shell."""
    from hostctl.shell._flavour import ShellSession

    process = _Process()
    session = ShellSession(POSIX_SHELL, process)
    session.send(script)

    assert process.written == [expected]


def test_zsh_quotes_a_leading_equals():
    """zsh expands `=word` to the path of `word`; sh does not.

    `shlex.quote` implements sh's rules, so `=report.txt` went through bare
    and zsh replaced it with a program path or failed outright.
    """
    assert ZSH.quote("=report.txt") == "'=report.txt'"
    assert POSIX_SHELL.quote("=report.txt") == "=report.txt"
    # Everything sh already quoted is untouched.
    assert ZSH.quote("a b") == POSIX_SHELL.quote("a b")


def test_powershell_7_is_named_pwsh_even_with_an_explicit_executable():
    """Identity comes from the version; the executable only moves the path."""
    flavour = PowerShellFlavour(7, executable="/opt/microsoft/powershell/7/pwsh")
    assert flavour.name == "pwsh"
    assert flavour.default_executable == "/opt/microsoft/powershell/7/pwsh"

    assert PowerShellFlavour(5).name == "powershell"
    assert PowerShellFlavour(7).default_executable == "pwsh"


def test_an_empty_env_is_not_an_empty_environment():
    """`env={}` means "no overrides", not "start the child with nothing".

    Forwarded to `subprocess` as `env={}`, it produced a child with no
    `PATH`, no `HOME` and no `SystemRoot`.
    """
    executor = _Recorder()
    Shell(POSIX_SHELL, executor, env={}).run("echo hi")

    assert executor.calls[0][2].get("env") is None


@pytest.mark.parametrize(
    "value",
    (bytearray(b"payload"), memoryview(b"payload")),
)
def test_normalize_input_covers_the_buffer_protocol(value):
    """The deadlock this function prevents, for the types it skipped.

    A `bytearray` reached a text-mode writer thread untouched and raised
    there -- the thread died without closing the pipe, the child never saw
    EOF, and `timeout=` could not fire because nothing was waiting.
    """
    assert normalize_input(value, text_mode=True) == "payload"
    assert normalize_input(value, text_mode=False) == b"payload"
    assert isinstance(normalize_input(value, text_mode=False), bytes)


@pytest.mark.skipif(shutil.which("cmd.exe") is None, reason="requires cmd.exe")
def test_a_cmd_executable_with_a_space_is_launchable(tmp_path):
    """The program slot is read by CreateProcess, not by cmd.

    Rendered with cmd's own caret quoting, `^"C:\\Program Files\\...^"` made
    Windows look for a program named `^` -- measured as
    `FileNotFoundError: [WinError 2]`.
    """
    directory = tmp_path / "with a space"
    directory.mkdir()
    spaced = directory / "my cmd.exe"
    shutil.copy(os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe"), spaced)

    rendered = CMD.command(("echo ok",), executable=str(spaced)).command
    assert rendered.startswith(f'"{spaced}"')

    completed = subprocess.run(rendered, capture_output=True, text=True)
    assert completed.returncode == 0
    assert completed.stdout.strip() == "ok"


def test_a_cmd_executable_containing_a_quote_is_refused():
    with pytest.raises(ValueError, match="quote"):
        CMD.command(("echo ok",), executable='c:\\a"b\\cmd.exe')


@pytest.mark.parametrize(
    ("command", "expected"),
    (
        (
            Cmdlet("Remove-Item", PSParam("-LiteralPath"), r"C:\tmp\x.txt"),
            r"& 'Remove-Item' -LiteralPath 'C:\tmp\x.txt'",
        ),
        (("prog.exe", PSParam("-Path:"), "v"), "& 'prog.exe' -Path: 'v'"),
        # A plain string is a value, however much it looks like a name.
        (
            Cmdlet("Get-Item", "-LiteralPath", "v"),
            "& 'Get-Item' '-LiteralPath' 'v'",
        ),
        (Cmdlet("prog", "-not a name"), "& 'prog' '-not a name'"),
        # The first element is the command, never a parameter name.
        (("-Weird",), "& '-Weird'"),
    ),
)
def test_powershell_binds_a_named_parameter(command, expected):
    """A quoted string is not a parameter name to PowerShell's binder.

    Measured against a real powershell.exe: `Remove-Item -LiteralPath`
    rendered as three string literals returned 1, reported "A positional
    parameter cannot be found", and left the file in place. `PSParam` is the
    one spelling that renders a name bare; nothing is inferred from the shape
    of a string.
    """
    assert PowerShellFlavour(7).command_text(command) == expected


def test_raw_shell_source_may_span_lines():
    """Raw text is shell source, and a newline there is ordinary syntax.

    Rejecting every control character in raw source made a heredoc, an
    `if`/`for` block and a literal tab unexpressible.
    """
    script = "if [ -d /srv/app ]; then\n  make deploy\nfi"
    assert POSIX_SHELL.script((script,)) == script
    assert POSIX_SHELL.script(("cut -d'\t' -f2 data.tsv",)) == "cut -d'\t' -f2 data.tsv"

    with pytest.raises(ValueError, match="control characters"):
        POSIX_SHELL.script(("echo \x00",))
    with pytest.raises(ValueError, match="control characters"):
        POSIX_SHELL.quote("a\nb")


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None, reason="requires powershell.exe"
)
@pytest.mark.parametrize(
    "value",
    ("", 'q"x', 'a b" c d', "trail sp\\", "a & b", "$env:PATH", "'quoted'"),
)
@pytest.mark.parametrize("version", (5, 7))
def test_a_powershell_structured_argument_reaches_a_real_program(value, version):
    """Every PowerShell assertion in the suite was on rendered text.

    The ones that ran used `Write-Output` -- a cmdlet, whose binder never
    sees the native-command re-quoting PowerShell 5 applies. This sends the
    argument to a real native program and asks the child what it got.
    """
    from hostctl.shell._powershell import PowerShellFlavour

    flavour = PowerShellFlavour(version)
    if shutil.which(flavour.default_executable) is None:
        pytest.skip(f"{flavour.default_executable} is unavailable")
    code = "import sys; print(ascii(sys.argv[1]))"
    rendered = flavour.command(((sys.executable, "-c", code, value),))

    completed = subprocess.run(
        rendered.command, capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr[:400]
    assert completed.stdout.strip() == ascii(value)


def test_a_flavour_names_the_parser_each_token_meets():
    """One hook answered two questions, and each flavour had its own answer.

    `quote()` is shell-syntactic; `argument(value, target=...)` is for
    whatever reads the token next. On POSIX those coincide -- `execve` takes
    a vector, nothing re-parses -- so every target renders alike. On Windows
    they genuinely differ, which is what the distinction exists for.
    """
    from hostctl.shell._flavour import ShellTarget

    value = "a b.txt"
    posix = {
        target: POSIX_SHELL.argument(value, target=target) for target in ShellTarget
    }
    assert set(posix.values()) == {POSIX_SHELL.quote(value)}

    windows = {target: CMD.argument(value, target=target) for target in ShellTarget}
    # cmd's three parsers must not agree: a builtin operand is quoted so cmd
    # stops splitting it, a native argument carries the caret layer through
    # to the child's C runtime, and the program slot is plain C quoting.
    assert windows[ShellTarget.SHELL] == '"a b.txt"'
    assert windows[ShellTarget.NATIVE] == '^"a b.txt^"'
    assert windows[ShellTarget.PROGRAM] == '"a b.txt"'
    assert windows[ShellTarget.SHELL] != windows[ShellTarget.NATIVE]


def test_powershell_5_escapes_for_a_native_line_and_not_for_a_cmdlet():
    """PowerShell 5 rebuilds a NATIVE command line; a cmdlet call is not one.

    Applied to a cmdlet argument the C-runtime layer would be visible in the
    bound parameter.
    """
    from hostctl.shell._powershell import PowerShellFlavour
    from hostctl.shell._flavour import ShellTarget

    five = PowerShellFlavour(5)
    value = 'a "quoted" value'

    assert five.argument(value, target=ShellTarget.NATIVE) != five.quote(value)
    assert five.argument(value, target=ShellTarget.CMDLET) == five.quote(value)

    seven = PowerShellFlavour(7)
    # 7 does not rebuild the line, so there is nothing to compensate for.
    assert seven.argument(value, target=ShellTarget.NATIVE) == seven.quote(value)


def test_the_target_is_inferred_where_it_is_decidable():
    """cmd knows its own builtin list; PowerShell cannot know a cmdlet."""
    assert CMD.command_target(("del", "/q", "x")) is ShellTarget.SHELL
    assert CMD.command_target(("tool.exe", "x")) is ShellTarget.NATIVE
    assert CMD.command_target(("TOOL.COM", "x")) is ShellTarget.NATIVE
    assert CMD.command_target(("run.bat", "x")) is ShellTarget.BATCH
    assert CMD.command_target((r"C:\a\Run.CMD", "x")) is ShellTarget.BATCH
    # A bare name, or a script type PATHEXT knows, may be a batch file.
    assert CMD.command_target(("git", "x")) is ShellTarget.UNRESOLVED
    assert CMD.command_target(("tool.ps1", "x")) is ShellTarget.UNRESOLVED
    # PowerShell cannot know a cmdlet from a program: only a suffix decides.
    assert POWERSHELL.command_target(("Remove-Item", "x")) is ShellTarget.UNRESOLVED
    assert POWERSHELL.command_target(("tool.exe", "x")) is ShellTarget.NATIVE
    assert POWERSHELL.command_target(("run.cmd", "x")) is ShellTarget.BATCH


def test_the_cwd_guard_covers_the_whole_payload_by_declaration():
    """Declared, not inferred from `context_order`'s ordering.

    PowerShell's order puts `env` between `cwd` and `command`, and its
    `join_cwd` override must still apply.
    """
    from hostctl.shell import BASH, FISH
    from hostctl.shell._powershell import PowerShellFlavour

    # Fused onto a GROUPED payload: the AND has to cover `b`, not just `a`.
    for flavour in (POSIX_SHELL, BASH, FISH, CMD):
        assert flavour.cwd_guard == "operator"
        script = flavour.script(("a", "b"), cwd="/srv/app")
        assert flavour.group(flavour.join(("a", "b"))) in script

    # PowerShell guards statement-wise, which is why it has no `join_cwd`.
    for flavour in (PowerShellFlavour(5), PowerShellFlavour(7)):
        assert flavour.cwd_guard == "statement"
        script = flavour.script(("a", "b"), cwd="/srv/app")
        assert "-ErrorAction Stop" in script
        assert "&&" not in script


def test_an_operator_guard_without_a_command_slot_is_refused():
    """The inference failed silently; the declaration cannot.

    A flavour declaring `operator` but leaving `command` out of
    `context_order` would render a cwd change and drop the payload. That
    was reachable before only by accident of ordering, and unreported.
    """

    class _Broken(type(POSIX_SHELL)):
        name = "broken"
        context_order = ("env", "cwd")

    with pytest.raises(ValueError, match="context_order"):
        _Broken().script(("a",), cwd="/srv")


@pytest.mark.parametrize(
    ("value", "token"),
    (
        ("plain", '"plain"'),
        ("a b", '"a b"'),
        ("", '""'),
        ('say "hi"', '"say ""hi"""'),
        ("100%", '"100%%cd:~,%"'),
        ("%OS%", '"%%cd:~,%OS%%cd:~,%"'),
        ("trail\\", '"trail\\\\"'),
        ('a\\"b', '"a\\\\""b"'),
        ("x&y|z<w>q^r", '"x&y|z<w>q^r"'),
    ),
)
def test_a_batch_argument_is_one_quoted_token(value, token):
    """A batch file's `%*` is spliced into a line cmd parses again.

    Inside the span cmd keeps `& | < > ^` as data, `%` is written as an
    expansion of an empty variable's slice so it survives the second
    percent pass, and `""` is the only quote cmd keeps balanced.
    """
    assert CMD.argument(value, target=ShellTarget.BATCH) == token


def test_a_batch_command_uses_the_batch_token_for_every_argument():
    rendered = CMD.structured_command((r"C:\tools\run.bat", 'a"b', "c d"))

    assert rendered == 'C:\\tools\\run.bat "a""b" "c d"'


def test_a_program_with_a_space_is_quoted_plainly_in_cmd():
    """The first element is read as a command word, not as argv data."""
    rendered = CMD.structured_command((r"C:\Program Files\App\app.exe", "x"))

    assert rendered == '"C:\\Program Files\\App\\app.exe" x'


def test_a_program_with_a_space_is_a_literal_in_powershell():
    for flavour in (POWERSHELL, PWSH):
        rendered = flavour.structured_command((r"C:\Program Files\App\app.exe", "x"))
        assert rendered == "& 'C:\\Program Files\\App\\app.exe' 'x'"


def test_cmd_refuses_a_quote_for_a_program_it_cannot_classify():
    with pytest.raises(ValueError, match="batch file"):
        CMD.structured_command(("git", 'say "hi"'))
    # Nothing a batch re-parse could act on: `%`, `^` and `!` corrupt but
    # cannot start a command, so they are not refused.
    assert CMD.structured_command(("git", "100%")) == "git " + CMD.quote("100%")
    # A program known to be native keeps the quote.
    assert CMD.structured_command(("tool.exe", 'say "hi"'))


@pytest.mark.parametrize("value", ('a"b', "a&b", "a|b", "a<b", "a>b"))
@pytest.mark.parametrize("flavour", (POWERSHELL, PWSH))
def test_powershell_refuses_a_batch_active_value_for_an_unclassified_program(
    flavour, value
):
    """PowerShell passes a value with no space unquoted, so a batch file
    behind a bare name would execute the part after the metacharacter."""
    with pytest.raises(ValueError, match="batch file"):
        flavour.structured_command(("hcshim", value))
    # The suffix says what it is, and each target has its own rule.
    assert flavour.structured_command(("hcshim.exe", value))
    assert flavour.structured_command(("hcshim.bat", value))
    # A cmdlet is its own spelling and nothing is refused.
    assert flavour.command_text(Cmdlet("Write-Output", value))


def test_powershell_batch_argument_is_a_literal_of_the_batch_token():
    for flavour in (POWERSHELL, PWSH):
        rendered = flavour.structured_command(("run.cmd", 'x" & y', "100%"))
        assert rendered == "& 'run.cmd' '\"x\"\" & y\"' '\"100%%cd:~,%\"'"


def test_pwsh_refuses_a_batch_argument_its_binder_would_quote_again():
    """pwsh does not count a quote that follows a backslash, so for
    `q\\\\" x` it sees an even count at the space and wraps the token."""
    with pytest.raises(ValueError, match="second time"):
        PWSH.structured_command(("run.cmd", 'q\\\\" x'))
    # Windows PowerShell counts every quote and leaves it alone.
    assert POWERSHELL.structured_command(("run.cmd", 'q\\\\" x'))


@pytest.mark.parametrize(
    "value",
    ('x" y', 'report" /MIR', '" "', '"a b"', 'a\\"b c', 'k" v w'),
)
def test_powershell_5_refuses_what_its_binder_would_split(value):
    """PS 5 wraps a native argument only when whitespace sits at an even
    count of quotes; with `\\"` as the only literal-quote spelling, a value
    whose every space follows an odd count cannot be passed at all."""
    with pytest.raises(ValueError, match="PWSH"):
        POWERSHELL.structured_command(("tool.exe", value))
    # pwsh passes it as one argument.
    assert PWSH.structured_command(("tool.exe", value))


@pytest.mark.parametrize(
    "value",
    ('my file" /MIR "z', 'a b" c', 'one " two " three', "a b", "plain", ""),
)
def test_powershell_5_still_passes_a_value_its_binder_wraps(value):
    assert POWERSHELL.structured_command(("tool.exe", value))


def test_a_plain_string_is_never_a_parameter_name():
    rendered = PWSH.structured_command(("tool.exe", "-Force"))

    assert rendered == "& 'tool.exe' '-Force'"


def test_psparam_validates_its_name_and_is_not_iterable():
    import collections.abc

    assert PSParam("-Path:").name == "-Path:"
    for bad in ("Path", "-a b", "-1x", "--x", "-a'b", ""):
        with pytest.raises(ValueError):
            PSParam(bad)
    assert not isinstance(PSParam("-Force"), collections.abc.Iterable)
    assert not isinstance(Cmdlet("Get-Item"), collections.abc.Iterable)


def test_cmdlet_arguments_are_scalar_values():
    with pytest.raises(TypeError):
        Cmdlet("Get-Item", ["a"])
    with pytest.raises(TypeError):
        Cmdlet("Get-Item", 3)
    with pytest.raises(TypeError):
        Cmdlet("")


def test_other_shells_refuse_the_powershell_markers():
    for flavour in (POSIX_SHELL, ZSH, CMD):
        with pytest.raises(TypeError):
            flavour.script([Cmdlet("Get-Item", "x")])
        with pytest.raises(TypeError):
            flavour.script([("prog", PSParam("-Force"))])


def test_a_cmdlet_takes_no_c_runtime_escaping():
    """No native program re-parses a cmdlet line, so a quote stays a quote."""
    value = 'a "quoted" value'

    assert POWERSHELL.command_text(Cmdlet("Write-Output", value)) == (
        "& 'Write-Output' 'a \"quoted\" value'"
    )


@pytest.mark.parametrize(
    ("flavour", "target", "expected"),
    (
        (CMD, r"C:\a b\out file.txt", 'echo hi > "C:\\a b\\out file.txt"'),
        (PWSH, r"C:\a b\out file.txt", "echo hi > 'C:\\a b\\out file.txt'"),
        (POSIX_SHELL, "/a b/out", "echo hi > '/a b/out'"),
    ),
)
def test_a_redirect_operand_is_quoted_for_the_shell_not_for_a_child(
    flavour, target, expected
):
    """The file name after `>` is read by the shell. cmd's caret form for a
    child's C runtime wrote `dir` and not `dir with space\\out file.txt`."""
    path = flavour.path_flavor(target)

    for operand in (path, (path,)):
        rendered = flavour.script(["echo hi", ShellOperator.REDIRECT, operand])
        assert rendered.startswith(expected)

    appended = flavour.script(["echo hi", ShellOperator.APPEND, path])
    assert appended.startswith(expected.replace(" > ", " >> "))


def test_a_redirect_operand_may_be_raw_text_and_never_several_words():
    assert CMD.script(["echo hi", ShellOperator.REDIRECT, "NUL"]) == "echo hi > NUL"
    with pytest.raises(ValueError, match="one file name"):
        CMD.script(["echo hi", ShellOperator.REDIRECT, ("a", "b")])


def test_windows_powershell_redirects_a_file_name_literally():
    """`>` is `Out-File -FilePath`, which globbed `[ab]` in the name."""
    path = PureWindowsPath(r"C:\a b\[ab]\out.txt")

    for operand in (path, (path,), (str(path),)):
        assert POWERSHELL.join(["echo hi", ShellOperator.REDIRECT, operand]) == (
            "echo hi | Out-File -LiteralPath 'C:\\a b\\[ab]\\out.txt' -Encoding Unicode"
        )
    assert POWERSHELL.join(["echo hi", ShellOperator.APPEND, path]) == (
        "echo hi | Out-File -LiteralPath 'C:\\a b\\[ab]\\out.txt' "
        "-Encoding Unicode -Append"
    )
    # Raw text after `>` is PowerShell source, `$null` included.
    assert POWERSHELL.join(["echo hi", ShellOperator.REDIRECT, "$null"]) == (
        "echo hi > $null"
    )
    # pwsh writes a native program's bytes untouched only through `>`.
    assert PWSH.join(["echo hi", ShellOperator.REDIRECT, path]) == (
        "echo hi > 'C:\\a b\\[ab]\\out.txt'"
    )


def test_windows_powershell_escapes_wildcards_in_an_absolute_program_path():
    """PowerShell 5 resolved `C:\\x\\[ab]\\t.cmd` as a pattern and ran
    `C:\\x\\a\\t.cmd`; pwsh reads the path literally and would not find an
    escaped one."""
    program = r"C:\x\q`[ab]\t.exe"

    assert POWERSHELL.structured_command((program, "v")) == (
        "& 'C:\\x\\q```[ab`]\\t.exe' 'v'"
    )
    assert POWERSHELL.script([PureWindowsPath(r"C:\x\[ab]\t.exe")]).startswith(
        "& 'C:\\x\\`[ab`]\\t.exe'"
    )
    assert PWSH.structured_command((program,)) == "& 'C:\\x\\q`[ab]\\t.exe'"
    # A backtick alone is not read as an escape; a relative path cannot be.
    assert POWERSHELL.structured_command((r"C:\q`x\t.exe",)) == "& 'C:\\q`x\\t.exe'"
    assert POWERSHELL.structured_command((r".\[ab]\t.exe",)) == "& '.\\[ab]\\t.exe'"


def test_pwsh_renders_captured_output_as_plain_text():
    """pwsh colours tables and errors for a colour TERM, even into a pipe."""
    plain = "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }"

    assert PWSH.invocation("x", executable="pwsh")[-1] == plain + ";x"
    assert plain in PWSH.command(["x"], stdin_free=True).stdin
    assert all(plain not in part for part in POWERSHELL.invocation("x"))
    assert plain not in POWERSHELL.command(["x"], stdin_free=True).stdin


def test_a_standalone_path_is_a_one_element_command():
    """PowerShell prints a quoted string; it needs the call operator to run."""
    exe = PureWindowsPath(r"C:\Windows\System32\whoami.exe")

    assert POWERSHELL.script([exe]).startswith("& 'C:\\Windows\\System32\\whoami.exe'")
    assert CMD.script([PureWindowsPath(r"C:\Program Files\a.exe")]) == (
        '"C:\\Program Files\\a.exe"'
    )
    assert POSIX_SHELL.script([PurePosixPath("/a b")]) == "'/a b'"


def test_cmd_start_always_gets_an_empty_title():
    """`start` reads its first quoted word as a window title, so a quoted
    program ran as a title and nothing started."""
    rendered = CMD.structured_command(("start", "/b", "/wait", r"C:\d s\x.bat", "a b"))

    assert rendered == 'start "" /b /wait ^"C:\\d s\\x.bat^" ^"a b^"'


@pytest.mark.parametrize("flavour", (POWERSHELL, PWSH))
def test_a_native_stop_parsing_argument_is_refused(flavour):
    """No spelling of the value `--%` reaches a native program as data."""
    with pytest.raises(ValueError, match="stop-parsing"):
        flavour.structured_command((r"C:\Windows\System32\whoami.exe", "--%"))
    # A cmdlet argument is not a native command line.
    assert flavour.command_text(Cmdlet("Write-Output", "--%"))


def test_the_powershell_epilogue_defers_to_set_should_exit():
    assert "$host.SetShouldExit(" in POWERSHELL.execution_epilogue
    assert "exit $" not in POWERSHELL.execution_epilogue


def test_a_relative_cwd_is_spelled_so_cdpath_cannot_redirect_it():
    from hostctl.shell import FISH

    for flavour in (POSIX_SHELL, ZSH, FISH):
        assert flavour.script(["x"], cwd="sub").startswith("cd -- ./sub")
        assert flavour.script(["x"], cwd="a/b").startswith("cd -- ./a/b")
        assert flavour.script(["x"], cwd="../up").startswith("cd -- ../up")
        assert flavour.script(["x"], cwd="/abs").startswith("cd -- /abs")
    assert POSIX_SHELL.script(["x"], cwd="sub").startswith("cd -- ./sub&&")


def test_a_command_after_a_heredoc_starts_on_a_new_line():
    """`EOF;echo next` leaves the here-document open and swallows the rest."""
    heredoc = "cat <<EOF\nhello\nEOF"

    assert POSIX_SHELL.script([heredoc, "echo AFTER"]) == heredoc + "\necho AFTER"
    assert POSIX_SHELL.script(["echo a", "echo b"]) == "echo a;echo b"
    assert POSIX_SHELL.script([heredoc], cwd="/srv") == (
        "cd -- /srv&&{ " + heredoc + "\n}"
    )


def test_session_send_writes_the_script_and_a_newline_and_nothing_else():
    class _Sink:
        written = ""

        def write(self, data):
            self.written += data

    for line, expected in (
        ("cat <<EOF\nhello\nEOF", "cat <<EOF\nhello\nEOF\n"),
        ("echo a |", "echo a |\n"),
        ("echo hi", "echo hi\n"),
    ):
        sink = _Sink()
        ShellSession(POSIX_SHELL, sink).send(line)
        assert sink.written == expected


def test_fish_quotes_a_word_that_fish_would_expand_to_a_pid():
    from hostctl.shell import FISH

    assert FISH.quote("%self") == "'%self'"
    assert FISH.quote("a%self") == "'a%self'"


def test_cmd_structured_call_is_refused():
    with pytest.raises(ValueError, match="call"):
        CMD.structured_command(("call", "x.bat"))
