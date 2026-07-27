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


@pytest.mark.parametrize(
    "value, encoding, expected",
    [
        # The reported failure: bytes with a text encoding used to kill
        # subprocess's writer thread and then block forever -- `timeout=` does
        # not fire, because nothing is waiting on the child. On 3.9 the same
        # call raised TypeError instead, so the behaviour was also
        # interpreter-dependent.
        (b"zz", "utf-8", "zz"),
        ("zz", "utf-8", "zz"),
        (b"zz", None, b"zz"),
        ("zz", None, b"zz"),
    ],
    ids=["bytes-text", "str-text", "bytes-binary", "str-binary"],
)
def test_local_input_is_normalized_to_the_stream_mode(value, encoding, expected):
    from hostctl.executor import LocalExecutor

    result = LocalExecutor()(
        sys.executable,
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
        input=value,
        encoding=encoding,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert result.stdout == expected


@pytest.mark.parametrize(
    "value, text_mode, expected",
    [
        (b"zz", True, "zz"),
        ("zz", True, "zz"),
        (b"zz", False, b"zz"),
        ("zz", False, b"zz"),
        (None, True, None),
        (None, False, None),
        # An int is a file descriptor, not payload, and must pass through.
        (subprocess.DEVNULL, True, subprocess.DEVNULL),
    ],
)
def test_normalize_input_matches_the_requested_mode(value, text_mode, expected):
    from hostctl.executor._common import normalize_input

    assert normalize_input(value, text_mode=text_mode) == expected


def test_normalize_input_honours_encoding_and_errors():
    from hostctl.executor._common import normalize_input

    # Undecodable bytes would raise under strict; the caller's policy applies.
    assert normalize_input(b"\xff", text_mode=True, errors="replace") == "�"
    assert normalize_input("é", text_mode=False, encoding="latin-1") == b"\xe9"


def test_local_shell_execute_path_preserves_native_arguments():
    result = LocalHost().shell.execute(
        Path(sys.executable),
        "-c",
        "import sys; print(repr(sys.argv[1]))",
        "a & b",
        text=True,
    )

    assert result.stdout.strip() == "'a & b'"
