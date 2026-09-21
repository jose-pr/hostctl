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


def test_local_env_is_additive_like_every_other_transport():
    """contracts.md states one rule: `env` is additive to the provider's.

    `subprocess.run(env=...)` replaces instead, so a SystemHost falling back
    to the local provider handed the child no PATH where the same call over
    SSH kept it -- and on Windows an environment without PATH/SystemRoot stops
    powershell.exe from starting at all.
    """
    import os
    import sys

    host = LocalHost()
    probe = (
        "import os, sys; "
        "print(os.environ.get('HOSTCTL_MARK'), bool(os.environ.get('PATH')))"
    )

    result = host.run(
        [sys.executable, "-c", probe], env={"HOSTCTL_MARK": "yes"}, check=False
    )

    assert result.stdout.split() == [b"yes", b"True"]
    assert "HOSTCTL_MARK" not in os.environ, "the parent environment is untouched"


def test_local_env_overrides_an_inherited_value():
    import os
    import sys

    host = LocalHost()
    os.environ["HOSTCTL_OVERRIDE_ME"] = "parent"
    try:
        result = host.run(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ['HOSTCTL_OVERRIDE_ME'])",
            ],
            env={"HOSTCTL_OVERRIDE_ME": "child"},
            check=False,
        )
    finally:
        del os.environ["HOSTCTL_OVERRIDE_ME"]

    assert result.stdout.strip() == b"child"


def test_a_shell_rendered_timeout_bounds_the_call_and_kills_the_tree(tmp_path):
    """`timeout=` bounded the shell, not the work.

    Every non-`Exec` command runs through a shell, so the real process is a
    grandchild holding the stdout/stderr pipes. `subprocess.run` kills only
    the direct child and then waits for those pipes to close: on Windows the
    call blocked for the command's full duration and returned the *completed*
    output as a timeout, and on POSIX the grandchild was orphaned and kept
    running, so a retry ran a second concurrent copy.

    The timeout is deliberately larger than a shell's own startup: with a
    shorter one the shell is killed before it has even spawned the payload,
    which is the one case the old code handled.
    """
    import time

    ticks = tmp_path / "ticks"
    code = (
        "import pathlib, time; p = pathlib.Path({0!r}); "
        "list(map(lambda n: (p.write_text(str(n)), time.sleep(0.1)), range(120)))"
    ).format(str(ticks))

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        LocalHost().run([sys.executable, "-c", code], timeout=3)
    elapsed = time.monotonic() - started

    assert elapsed < 8, f"timeout=3 took {elapsed:.1f}s"
    assert ticks.exists(), "the payload never started; the test proves nothing"
    frozen = ticks.read_text()
    time.sleep(1.5)
    assert ticks.read_text() == frozen, "the grandchild outlived its timeout"
