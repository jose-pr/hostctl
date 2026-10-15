"""One value list, every shell slot, executed in the real interpreters.

The two worst quoting bugs this package shipped both passed a green suite: each
earlier fix pinned ten or so hand-picked values of the reported shape, and the
next shape went untested. This module runs ONE list of values (`_adversarial`)
through every slot a value can occupy -- argument, program, redirect operand,
environment value, working directory, a persistent session -- in the shell that
really parses it, and compares what arrived with what was sent.

`KNOWN_DEFECTS` names every failing cell by the finding that explains it; an
item asserts `failing == predicted`, so fixing a defect fails the item until
its row is deleted, and a new failure fails it outright. `DOCUMENTED` holds
the cells a flavour refuses on purpose, each naming its ledger row in
`docs/guide/contracts.md`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import typing
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from _adversarial import (
    POSIX_HOSTILE,
    VALUES,
    WINDOWS_HOSTILE,
    fs_names,
)
from hostctl.executor import CommandLine, LocalExecutor
from hostctl.process import Process
from hostctl.shell.cmd import _batch_token
from hostctl.shell.powershell import _binder_wraps, _crt_escape
from hostctl.shell import (
    BASH,
    CMD,
    FISH,
    POSIX_SHELL,
    POWERSHELL,
    PWSH,
    ZSH,
    ShellOperator,
    ShellSession,
)

NT = os.name == "nt"
CHUNK = 25
TIMEOUT = 180
#: Without the venv redirector: one process less per child, and the children
#: only need `json`, `os` and `sys`.
PY = getattr(sys, "_base_executable", None) or sys.executable

Predicate = typing.Callable[[str, str, str], bool]

#: A chunk fails although both halves pass: one value changes how its
#: neighbour is parsed (an unbalanced quote carries over). Named as a cell so a
#: known defect can predict it and an unknown one fails the item.
INTERFERENCE = "<interference>"


def _ps5_stuck(value: str) -> bool:
    """PowerShell 5 cannot pass `value` to a native program unsplit.

    It wraps a native argument in quotes only when whitespace sits at an even
    count of `"` (counting the `\\"` this package emits), so a value whose
    every whitespace follows an odd count reaches the child split.
    """
    token = _crt_escape(value)
    if not any(char.isspace() for char in token):
        return False
    quotes = 0
    for char in token:
        if char == '"':
            quotes += 1
        elif char.isspace() and quotes % 2 == 0:
            return False
    return True


def _pwsh_rewraps(value: str) -> bool:
    """pwsh would quote the batch token of `value` a second time."""
    return _binder_wraps(_batch_token(value), major_version=7)


_PERCENT_NAME = re.compile(r"%[A-Za-z_]+%")
_PS = ("powershell", "pwsh")

#: finding id -> predicate over (flavour, slot, value). Every failing cell of
#: the corpus must be named here; a new failure is a new finding, not a row to
#: add without one.
KNOWN_DEFECTS: typing.Dict[str, Predicate] = {
    # pwsh's `>` takes a wildcard path for cmdlet output (`echo` is one).
    "new-ps-redirect-wildcard": lambda f, s, v: (
        f == "pwsh" and s == "redirect" and v == "[ab]"
    ),
}

#: Cells a flavour refuses or corrupts on purpose, each naming its ledger row
#: in docs/guide/contracts.md. A documented cell may raise or arrive changed;
#: it must never execute.
DOCUMENTED: typing.Dict[str, Predicate] = {
    # "PowerShell native argument `--%`"
    "ps-stop-parsing": lambda f, s, v: (
        f in _PS and s in ("argv_native", "argv_unresolved", "session") and v == "--%"
    ),
    # "Embedded `env` value `\"\"` under Windows PowerShell 5 and cmd"
    "empty-env-value": lambda f, s, v: (
        f in ("cmd", "powershell") and s == "env" and v == ""
    ),
    # "Windows PowerShell 5 native arguments"
    "ps5-native": lambda f, s, v: (
        f == "powershell" and s in ("argv_native", "session") and _ps5_stuck(v)
    ),
    # "Program of unknown type": cmd refuses a quote.
    "unresolved-cmd": lambda f, s, v: (
        f == "cmd" and s == "argv_unresolved" and '"' in v
    ),
    # "Program of unknown type": PowerShell refuses what a batch file would
    # run, and a batch file corrupts `^`, `%NAME%` and (under pwsh) an empty
    # argument without being able to execute them.
    "unresolved-ps": lambda f, s, v: (
        f in _PS
        and s == "argv_unresolved"
        and (
            any(char in v for char in '"&|<>^')
            or _PERCENT_NAME.search(v) is not None
            or (f == "pwsh" and v == "")
        )
    ),
    # "Batch files": pwsh's binder wraps a token twice when a quote follows a
    # backslash.
    "pwsh-batch-rewrap": lambda f, s, v: (
        f == "pwsh" and s == "argv_batch" and _pwsh_rewraps(v)
    ),
    # "Batch files": cmd expands %NAME% in the path of the batch file it runs.
    "batch-path-percent": lambda f, s, v: (
        f in _PS
        and s in ("program", "program_path")
        and _PERCENT_NAME.fullmatch(v) is not None
    ),
}

WINDOWS_FLAVOURS = {
    "cmd": (CMD, "cmd.exe"),
    "powershell": (POWERSHELL, "powershell.exe"),
    "pwsh": (PWSH, "pwsh"),
}
POSIX_FLAVOURS = {
    "posix": (POSIX_SHELL, "sh"),
    "bash": (BASH, "bash"),
    "zsh": (ZSH, "zsh"),
    "fish": (FISH, "fish"),
}


def _oem() -> str:
    if NT:
        import ctypes

        return "cp%d" % ctypes.windll.kernel32.GetOEMCP()  # type: ignore[attr-defined]
    return "utf-8"


# --------------------------------------------------------------------------
# running a rendered script in a real shell


class Runner:
    """One flavour bound to its interpreter."""

    def __init__(self, name, flavour, executable, family):
        self.name = name
        self.flavour = flavour
        self.executable = executable
        self.family = family

    @property
    def separator(self) -> str:
        return "&" if self.name == "cmd" else "\n"

    def render(self, cmds, **options) -> str:
        """The flavour's script without the status epilogue, which a joined
        multi-command script must not repeat."""
        script = self.flavour.script(cmds, **options)
        epilogue = self.flavour.execution_epilogue
        if epilogue and script.endswith(epilogue):
            script = script[: -len(epilogue)]
        return script

    def run_text(self, text: str, *, env=None, input=None):
        if self.name == "cmd":
            line = CMD.command([text]).command
            return LocalExecutor()(
                CommandLine(line), check=False, env=env, timeout=TIMEOUT
            )
        argv = self.flavour.invocation(text, executable=self.executable)
        return LocalExecutor()(*argv, check=False, env=env, timeout=TIMEOUT)

    def session_argv(self) -> typing.List[str]:
        if self.name == "cmd":
            return ["cmd.exe", "/d", "/q"]
        if self.family == "nt":
            return [self.executable, "-NoProfile", "-NonInteractive", "-Command", "-"]
        if self.name == "fish":
            return [self.executable]
        return [self.executable, "-s"]


class _PopenProcess(Process):
    """A buffered stand-in for a spawned shell: `LocalHost` has no `spawn`.

    Everything written is delivered at `send_eof()` and the output is read
    after the shell has exited, so the item cannot deadlock on a pipe.
    """

    def __init__(self, argv, env=None):
        self._argv = argv
        self._env = env
        self._written: typing.List[bytes] = []
        self._stdout = b""
        self._returncode: typing.Optional[int] = None

    @property
    def returncode(self):
        return self._returncode

    def write(self, data):
        self._written.append(data.encode("utf-8") if isinstance(data, str) else data)

    def send_eof(self):
        environment = None
        if self._env:
            environment = dict(os.environ)
            environment.update(self._env)
        with subprocess.Popen(
            self._argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
        ) as process:
            try:
                self._stdout, _ = process.communicate(
                    b"".join(self._written), timeout=TIMEOUT
                )
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            self._returncode = process.returncode

    def read(self, size=-1):
        return self._stdout

    def read_stderr(self, size=-1):
        return b""

    def resize(self, columns, rows, pixel_width=0, pixel_height=0):
        pass

    def wait(self, timeout=None):
        return self._returncode

    def terminate(self):
        pass

    def kill(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


# --------------------------------------------------------------------------
# the cell driver


class Case(typing.NamedTuple):
    value: str
    data: typing.Any = None
    marker: typing.Optional[Path] = None


class Outcome(typing.NamedTuple):
    values: typing.Set[str]
    refused: typing.Set[str]
    failed: typing.Set[str]
    #: Values that created their marker file, which is never acceptable.
    executed: typing.Set[str] = frozenset()  # type: ignore[assignment]


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def drive(cases, render, execute, *, chunk=CHUNK) -> Outcome:
    """Render and run `cases` in chunks, attributing a failed chunk by value.

    `render(group)` returns what `execute(rendered, group)` runs; a
    `ValueError` from it is a refusal. A chunk that fails is halved and re-run
    until single values are named, so only failing cells pay. A marker file
    existing afterwards means the value executed as a command.
    """
    refused, failed, executed = set(), set(), set()
    live = []
    run = execute

    def execute(text, group):  # noqa: F811 -- a failed launch is a failed cell
        try:
            return run(text, group)
        except (OSError, subprocess.SubprocessError):
            return False

    for case in cases:
        try:
            render([case])
        except ValueError:
            refused.add(case.value)
        else:
            live.append(case)

    def injected(group):
        for case in group:
            if case.marker is not None and case.marker.exists():
                failed.add(case.value)
                executed.add(case.value)

    def isolate(group):
        # `group` is known to fail: halve it until single values are named.
        if len(group) == 1:
            failed.add(group[0].value)
            return
        middle = len(group) // 2
        results = []
        for part in (group[:middle], group[middle:]):
            ok = execute(render(part), part)
            injected(part)
            if not ok:
                isolate(part)
            results.append(ok)
        if all(results):
            failed.add(INTERFERENCE)

    for group in _chunks(live, chunk):
        ok = execute(render(group), group)
        injected(group)
        if not ok:
            isolate(group)
    return Outcome(
        {case.value for case in cases} | {INTERFERENCE}, refused, failed, executed
    )


def settle(flavour: str, slot: str, outcome: Outcome) -> None:
    predicted = {
        value
        for value in outcome.values
        if any(rule(flavour, slot, value) for rule in KNOWN_DEFECTS.values())
    }

    def documented(value):
        return value not in outcome.executed and any(
            rule(flavour, slot, value) for rule in DOCUMENTED.values()
        )

    # A documented cell may be refused or arrive changed, never execute.
    failing = {
        value for value in outcome.failed | outcome.refused if not documented(value)
    }
    dump = os.environ.get("HC_CORPUS_DUMP")
    if dump:
        with open(dump, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    {
                        "item": "%s-%s" % (flavour, slot),
                        "failing": sorted(failing),
                        "refused": sorted(outcome.refused),
                        "total": len(outcome.values),
                    }
                )
                + "\n"
            )
    problems = []
    if failing - predicted:
        problems.append("UNEXPECTED failures: %r" % sorted(failing - predicted))
    if predicted - failing:
        problems.append("known defects that now pass: %r" % sorted(predicted - failing))
    assert not problems, "%s-%s: %s (of %d values, %d refused)" % (
        flavour,
        slot,
        "; ".join(problems),
        len(outcome.values),
        len(outcome.refused),
    )


# --------------------------------------------------------------------------
# fixtures and case builders


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    root = tmp_path_factory.mktemp("tools")
    dump = root / "dump.py"
    # Bytes, not write_text(newline=), which is 3.10+.
    dump.write_bytes(
        b"import json, sys\nsys.stdout.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    dumpenv = root / "dumpenv.py"
    dumpenv.write_bytes(
        b"import json, os, sys\n"
        b"sys.stdout.write(json.dumps([os.environ.get(n, '\\x00unset')"
        b" for n in sys.argv[1:]]) + '\\n')\n"
    )
    shim_body = ('@"%s" "%s" %%*\r\n' % (PY, dump)).encode("utf-8")
    spaced = root / "hc shim dir"
    spaced.mkdir()
    (spaced / "hc shim.cmd").write_bytes(shim_body)
    unresolved = root / "unresolved"
    unresolved.mkdir()
    (unresolved / "hcshim.cmd").write_bytes(shim_body)
    return {
        "dump": str(dump),
        "dumpenv": str(dumpenv),
        "shim": str(spaced / "hc shim.cmd"),
        "unresolved_dir": str(unresolved),
    }


@pytest.fixture(autouse=True)
def _contained(tmp_path, monkeypatch):
    """A value that escapes into a redirect writes into the temp directory."""
    monkeypatch.chdir(tmp_path)


def _hostile(family: str, markers: Path, start: int = 0) -> typing.List[Case]:
    markers.mkdir(parents=True, exist_ok=True)
    templates = WINDOWS_HOSTILE if family == "nt" else POSIX_HOSTILE
    cases = []
    for number, template in enumerate(templates, start):
        marker = markers / ("m%d" % number)
        cases.append(Case(template.format(marker=marker), marker=marker))
    return cases


def _value_cases(family: str, tmp_path: Path) -> typing.List[Case]:
    return [Case(value) for value in VALUES] + _hostile(family, tmp_path / "markers")


def _last_json(completed) -> typing.Any:
    lines = completed.stdout.decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("["):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def _pairs(group) -> typing.List[str]:
    flat: typing.List[str] = []
    for index, case in enumerate(group):
        flat.extend(("t%d" % index, case.value))
    return flat


def _lines(completed, strip=True) -> typing.List[str]:
    text = completed.stdout.decode(_oem(), "replace")
    if strip:
        return [line.strip() for line in text.splitlines() if line.strip()]
    return [line for line in text.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# slots


def slot_argv(runner: Runner, tools, tmp_path, program: str, env=None) -> Outcome:
    # A shim already names the script; only the interpreter takes it as an argument.
    lead = (tools["dump"],) if program == PY else ()

    def render(group):
        return runner.render([(program, *lead, *_pairs(group))])

    def execute(text, group):
        return _last_json(runner.run_text(text, env=env)) == _pairs(group)

    return drive(_value_cases(runner.family, tmp_path), render, execute)


def slot_command_line(runner: Runner, tools, tmp_path) -> Outcome:
    def render(group):
        return runner.flavour.command(
            [(PY, tools["dump"], *_pairs(group))], executable=runner.executable
        ).command

    def execute(line, group):
        completed = LocalExecutor()("sh", "-c", line, check=False, timeout=TIMEOUT)
        return _last_json(completed) == _pairs(group)

    return drive(_value_cases(runner.family, tmp_path), render, execute)


def _program_cases(runner: Runner, tmp_path: Path) -> typing.List[Case]:
    cases = []
    for number, name in enumerate(fs_names(runner.family)):
        directory = tmp_path / "programs" / name
        directory.mkdir(parents=True, exist_ok=True)
        token = "OK-%d" % number
        if runner.family == "nt":
            shutil.copyfile(
                os.path.join(os.environ["SystemRoot"], "System32", "hostname.exe"),
                directory / "hostname.exe",
            )
            (directory / "hc.cmd").write_bytes(("@echo %s\r\n" % token).encode())
            cases.append(Case(name, ("exe", directory / "hostname.exe", token)))
            cases.append(Case(name, ("cmd", directory / "hc.cmd", token)))
        else:
            script = directory / "hc"
            script.write_bytes(("#!/bin/sh\necho %s\n" % token).encode())
            script.chmod(0o755)
            cases.append(Case(name, ("sh", script, token)))
    return cases


def slot_program(runner: Runner, tmp_path, as_path: bool) -> Outcome:
    pure = PureWindowsPath if runner.family == "nt" else PurePosixPath

    def command(case):
        path = str(case.data[1])
        return pure(path) if as_path else (path,)

    def render(group):
        return runner.separator.join(runner.render([command(case)]) for case in group)

    def execute(text, group):
        expected = [
            socket.gethostname().casefold() if case.data[0] == "exe" else case.data[2]
            for case in group
        ]
        got = [line.casefold() for line in _lines(runner.run_text(text))]
        return got == [item.casefold() for item in expected]

    return drive(_program_cases(runner, tmp_path), render, execute)


def slot_redirect(runner: Runner, tmp_path) -> Outcome:
    pure = PureWindowsPath if runner.family == "nt" else PurePosixPath
    root = tmp_path / "redirect"
    cases = []
    for number, name in enumerate(fs_names(runner.family)):
        cases.append(Case(name, (number, root / ("r%d" % number), name)))

    def target(case):
        return case.data[1] / case.data[2] / "out file.txt"

    def render(group):
        pieces = []
        for case in group:
            path = pure(str(target(case)))
            pieces.append(runner.render(["echo hc", ShellOperator.REDIRECT, path]))
            pieces.append(runner.render(["echo hc", ShellOperator.APPEND, path]))
        return runner.separator.join(pieces)

    def execute(text, group):
        for case in group:
            shutil.rmtree(case.data[1], ignore_errors=True)
            (case.data[1] / case.data[2]).mkdir(parents=True)
        runner.run_text(text)
        ok = True
        for case in group:
            expected = target(case)
            found = {
                Path(base, name)
                for base, _, names in os.walk(case.data[1])
                for name in names
            }
            if found != {expected}:
                ok = False
                continue
            data = expected.read_bytes()
            if data.startswith((b"\xff\xfe", b"\xfe\xff")):
                content = data.decode("utf-16")
            else:
                content = data.decode("utf-8-sig", "replace")
            if [line.strip() for line in content.splitlines() if line.strip()] != [
                "hc",
                "hc",
            ]:
                ok = False
        return ok

    return drive(cases, render, execute)


def slot_env(runner: Runner, tools, tmp_path) -> Outcome:
    def render(group):
        names = ["HC_%d" % index for index in range(len(group))]
        env = {name: case.value for name, case in zip(names, group)}
        return runner.render([(PY, tools["dumpenv"], *names)], env=env)

    def execute(text, group):
        got = _last_json(runner.run_text(text))
        return got == [case.value for case in group]

    return drive(_value_cases(runner.family, tmp_path), render, execute)


def slot_cwd(runner: Runner, tmp_path) -> Outcome:
    printer = {
        "cmd": "cd",
        "powershell": "(Get-Location).ProviderPath",
        "pwsh": "(Get-Location).ProviderPath",
    }.get(runner.name, "pwd")
    cases = []
    for name in fs_names(runner.family):
        directory = tmp_path / "cwd" / name
        directory.mkdir(parents=True, exist_ok=True)
        cases.append(Case(name, directory))

    def render(group):
        return runner.separator.join(
            runner.render([printer], cwd=str(case.data)) for case in group
        )

    def normal(text):
        return os.path.normcase(os.path.realpath(text))

    def execute(text, group):
        got = [normal(line) for line in _lines(runner.run_text(text), strip=False)]
        return got == [normal(str(case.data)) for case in group]

    return drive(cases, render, execute)


def slot_cdpath(runner: Runner, tmp_path) -> Outcome:
    """A relative cwd must mean the process's directory, whatever CDPATH holds."""
    present_base = tmp_path / "cdpath" / "present"
    absent_base = tmp_path / "cdpath" / "absent"
    elsewhere = tmp_path / "cdpath" / "elsewhere"
    for base in (present_base, absent_base, elsewhere):
        base.mkdir(parents=True, exist_ok=True)
    cases = []
    for number, name in enumerate(fs_names("posix")):
        (elsewhere / name).mkdir(parents=True, exist_ok=True)
        (present_base / name).mkdir(parents=True, exist_ok=True)
        cases.append(Case(name, (number, True)))
        cases.append(Case(name, (number, False)))

    def tag(case):
        return "T%d%s" % (case.data[0], "p" if case.data[1] else "a")

    def render(group):
        pieces = []
        for case in group:
            base = present_base if case.data[1] else absent_base
            # printf, not echo: zsh's echo interprets backslashes in the path.
            echo = (
                "printf '%%s\\n' %s:(pwd)" % tag(case)
                if runner.name == "fish"
                else "printf '%%s\\n' \"%s:$(pwd)\"" % tag(case)
            )
            pieces.append(
                runner.flavour.change_directory(str(base))
                + runner.separator
                + runner.render([echo], cwd=case.value)
            )
        return runner.separator.join(pieces)

    def execute(text, group):
        completed = runner.run_text(text, env={"CDPATH": str(elsewhere)})
        got = {}
        for line in completed.stdout.decode("utf-8", "replace").splitlines():
            head, _, tail = line.partition(":")
            if head.startswith("T"):
                got[head] = tail
        expected = {
            tag(case): os.path.realpath(present_base / case.value)
            for case in group
            if case.data[1]
        }
        return {key: os.path.realpath(path) for key, path in got.items()} == expected

    # The same value appears twice (present and absent); one chunk holds both.
    return drive(cases, render, execute)


def _session_run(runner: Runner, sends, env=None) -> typing.List[str]:
    process = _PopenProcess(runner.session_argv(), env=env)
    session = ShellSession(runner.flavour, process)
    for command in sends:
        session.send(command)
    session.send_eof()
    out = process.read()
    text = out.decode("utf-8", "replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


def slot_session(runner: Runner, tools, tmp_path) -> Outcome:
    def keep(case):
        return runner.family != "nt" or case.value.isascii()

    cases = [case for case in _value_cases(runner.family, tmp_path) if keep(case)]

    def render(group):
        return runner.render([(PY, tools["dump"], *_pairs(group))])

    def execute(_text, group):
        lines = _session_run(runner, [(PY, tools["dump"], *_pairs(group))])
        for line in reversed(lines):
            # cmd prints its prompt, with no newline, before the output.
            start = line.find('["t0"')
            if start >= 0:
                try:
                    return json.loads(line[start:]) == _pairs(group)
                except ValueError:
                    return False
        return False

    outcome = drive(cases, render, execute)
    if runner.family == "nt":
        return outcome
    specials = {
        "<open-pipe>": (["echo a |", "cat"], ["a"]),
    }
    if runner.name != "fish":
        specials["<heredoc>"] = (
            ["cat <<EOF\nhello\nEOF", "echo AFTER-1", "echo AFTER-2"],
            ["hello", "AFTER-1", "AFTER-2"],
        )
    failed = set(outcome.failed)
    for label, (sends, expected) in specials.items():
        if _session_run(runner, sends) != expected:
            failed.add(label)
    return Outcome(
        outcome.values | set(specials), outcome.refused, failed, outcome.executed
    )


def slot_exec(tools, tmp_path, program: str) -> Outcome:
    def render(group):
        return None

    def execute(_text, group):
        lead = (tools["dump"],) if program == PY else ()
        completed = LocalExecutor()(
            program, *lead, *_pairs(group), check=False, timeout=TIMEOUT
        )
        return _last_json(completed) == _pairs(group)

    return drive(_value_cases("nt", tmp_path), render, execute)


# --------------------------------------------------------------------------
# the items


def _interpreter_mark(family: str, name: str, executable: str):
    if family == "nt" and not NT:
        return pytest.mark.skip(reason="needs Windows")
    if family == "posix" and NT:
        return pytest.mark.skip(reason="needs a POSIX host (WSL, Linux, macOS)")
    if shutil.which(executable) is None:
        return pytest.mark.skip(reason="%s is not installed" % executable)
    return None


def _params(table, family, slots):
    for name, (flavour, executable) in table.items():
        mark = _interpreter_mark(family, name, executable)
        for slot in slots:
            yield pytest.param(
                name,
                slot,
                id="%s-%s" % (name, slot),
                marks=[mark] if mark is not None else [],
            )


_WINDOWS_SLOTS = (
    "argv_native",
    "argv_batch",
    "argv_unresolved",
    "program",
    "program_path",
    "redirect",
    "env",
    "cwd",
    "session",
)
_POSIX_SLOTS = (
    "argv",
    "command_line",
    "program",
    "redirect",
    "env",
    "cwd",
    "cdpath",
    "session",
)


def _runner(table, family, name) -> Runner:
    flavour, executable = table[name]
    return Runner(name, flavour, shutil.which(executable) or executable, family)


@pytest.mark.parametrize(
    "name, slot", list(_params(WINDOWS_FLAVOURS, "nt", _WINDOWS_SLOTS))
)
def test_windows_corpus(name, slot, tools, tmp_path):
    runner = _runner(WINDOWS_FLAVOURS, "nt", name)
    if slot == "argv_native":
        outcome = slot_argv(runner, tools, tmp_path, PY)
    elif slot == "argv_batch":
        outcome = slot_argv(runner, tools, tmp_path, tools["shim"])
    elif slot == "argv_unresolved":
        path = tools["unresolved_dir"] + os.pathsep + os.environ.get("PATH", "")
        outcome = slot_argv(runner, tools, tmp_path, "hcshim", env={"PATH": path})
    elif slot == "program":
        outcome = slot_program(runner, tmp_path, as_path=False)
    elif slot == "program_path":
        outcome = slot_program(runner, tmp_path, as_path=True)
    elif slot == "redirect":
        outcome = slot_redirect(runner, tmp_path)
    elif slot == "env":
        outcome = slot_env(runner, tools, tmp_path)
    elif slot == "cwd":
        outcome = slot_cwd(runner, tmp_path)
    else:
        outcome = slot_session(runner, tools, tmp_path)
    settle(name, slot, outcome)


@pytest.mark.skipif(not NT, reason="needs Windows")
@pytest.mark.parametrize("slot", ("native", "batch"))
def test_windows_exec_corpus(slot, tools, tmp_path):
    program = PY if slot == "native" else tools["shim"]
    settle("exec", "exec_" + slot, slot_exec(tools, tmp_path, program))


@pytest.mark.parametrize(
    "name, slot", list(_params(POSIX_FLAVOURS, "posix", _POSIX_SLOTS))
)
def test_posix_corpus(name, slot, tools, tmp_path):
    runner = _runner(POSIX_FLAVOURS, "posix", name)
    if slot == "argv":
        outcome = slot_argv(runner, tools, tmp_path, PY)
    elif slot == "command_line":
        outcome = slot_command_line(runner, tools, tmp_path)
    elif slot == "program":
        outcome = slot_program(runner, tmp_path, as_path=False)
    elif slot == "redirect":
        outcome = slot_redirect(runner, tmp_path)
    elif slot == "env":
        outcome = slot_env(runner, tools, tmp_path)
    elif slot == "cwd":
        outcome = slot_cwd(runner, tmp_path)
    elif slot == "cdpath":
        outcome = slot_cdpath(runner, tmp_path)
    else:
        outcome = slot_session(runner, tools, tmp_path)
    settle(name, slot, outcome)


def test_every_flavour_refuses_a_control_character():
    for flavour in (POSIX_SHELL, BASH, ZSH, FISH, CMD, POWERSHELL, PWSH):
        with pytest.raises(ValueError):
            flavour.quote("a\x01b")
