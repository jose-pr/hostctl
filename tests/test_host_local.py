"""LocalHost command and path behavior."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pathlib_next import Path as NextPath

from hostctl import Host, LocalHost


def test_host_is_abstract():
    with pytest.raises(TypeError):
        Host()


def test_local_host_is_local():
    assert LocalHost().scheme == "local"
    assert LocalHost().capabilities == frozenset(("run", "path"))


def test_local_path_is_plain_pathlib(tmp_path):
    path = LocalHost().path(tmp_path, "foo.txt")
    assert isinstance(path, Path)
    assert isinstance(path, NextPath)
    assert path == tmp_path / "foo.txt"
    with pytest.raises(ValueError, match="local"):
        LocalHost().path(tmp_path, backend="sftp")


def test_local_run_captures_stdout_and_quotes_arguments():
    if sys.platform == "win32":
        result = LocalHost().run(["Write-Output", "a b"])
        assert result.stdout.strip() == b"a b"
    else:
        result = LocalHost().run(["printf", "%s", "a b"])
        assert result.stdout == b"a b"


def test_local_run_text_input_and_check():
    if sys.platform == "win32":
        command = "[Console]::Out.Write([Console]::In.ReadToEnd())"
    else:
        command = "cat"
    result = LocalHost().run(
        command, input="hi\n", encoding="utf-8", capture_output="stdout"
    )
    assert result.stdout == "hi\n"
    with pytest.raises(subprocess.CalledProcessError):
        LocalHost().run("exit 1")


def test_local_shell_execute_path_preserves_native_arguments():
    result = LocalHost().shell.execute(
        Path(sys.executable),
        "-c",
        "import sys; print(repr(sys.argv[1]))",
        "a & b",
        text=True,
    )

    assert result.stdout.strip() == "'a & b'"
