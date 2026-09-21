"""cmd rendering measured by what it does, not by what it looks like.

`echo` is the one builtin whose output is identical whether its arguments
were split or not, so a suite that only tests `echo` passes while
`host.run(["del", "/q", "a b.txt"])` deletes the wrong files. These cases
observe the filesystem and the environment instead.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from hostctl.shell import CMD

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires a real cmd.exe")


def _run(command, cwd=None):
    return subprocess.run(
        command.command,
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )


def test_a_spaced_argument_to_mkdir_makes_one_directory(tmp_path):
    """`mkdir` splits on whitespace, so a leaked escape makes two directories."""
    result = _run(CMD.command((("mkdir", "two words"),)), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert [entry.name for entry in tmp_path.iterdir()] == ["two words"]


def test_a_spaced_argument_to_del_removes_one_file(tmp_path):
    """Split, `del /q "keep me.txt"` deletes `keep` and `me.txt` instead."""
    (tmp_path / "keep me.txt").write_text("x", encoding="utf-8")
    (tmp_path / "keep").write_text("x", encoding="utf-8")
    (tmp_path / "me.txt").write_text("x", encoding="utf-8")

    result = _run(CMD.command((("del", "/q", "keep me.txt"),)), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["keep", "me.txt"]


def test_an_environment_value_reaches_the_child_verbatim(tmp_path):
    """A caret in the rendered `set` is an escape, not data.

    The old assertion checked for `^%` in the rendered text -- which is what
    corruption looks like as well as what escaping looks like. This one asks
    the child what it actually received.
    """
    code = "import os, sys; sys.stdout.write(os.environ['HX'])"
    command = CMD.command(
        ((sys.executable, "-c", code),),
        env={"HX": "100%"},
    )

    result = _run(command, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "100%"


@pytest.mark.parametrize(
    "value",
    ("", 'q"x', 'a b" c d', "trail sp\\", "%PATH%", "a & b", "^caret"),
)
def test_a_structured_argument_reaches_a_real_program_unchanged(value, tmp_path):
    code = "import sys; print(ascii(sys.argv[1]))"
    command = CMD.command(((sys.executable, "-c", code, value),))

    result = _run(command, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ascii(value)
