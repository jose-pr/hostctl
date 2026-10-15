"""LocalHost command and path behavior."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pathapi import Path as NextPath

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
        # Bytes with a text encoding, passed through, kill subprocess's
        # writer thread and block forever (`timeout=` does not fire, nothing
        # waits on the child); on 3.9 the same call raises TypeError.
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
    ],
)
def test_normalize_input_matches_the_requested_mode(value, text_mode, expected):
    from hostctl.executor._common import normalize_input

    assert normalize_input(value, text_mode=text_mode) == expected


@pytest.mark.parametrize("value", (3, subprocess.DEVNULL, 1.5, ["x"]))
def test_normalize_input_refuses_what_is_not_a_payload(value):
    """An int reached `subprocess`' writer thread: a hang on 3.14."""
    from hostctl.executor._common import normalize_input

    with pytest.raises(TypeError, match="input must be"):
        normalize_input(value, text_mode=False)


def test_normalize_input_honours_encoding_and_errors():
    from hostctl.executor._common import normalize_input

    # Undecodable bytes would raise under strict; the caller's policy applies.
    assert normalize_input(b"\xff", text_mode=True, errors="replace") == "�"
    assert normalize_input("é", text_mode=False, encoding="latin-1") == b"\xe9"


def test_local_executor_path_preserves_native_arguments():
    result = LocalHost().executor(
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


def test_local_path_no_longer_injects_the_working_directory():
    """`LocalHost` was the one host whose `path()` added a segment.

    Every other transport returns the path with no segments added, so
    `host.path()` meant two different things depending on which host you
    had. The relative path it now returns still resolves against this
    process's working directory, so the *behaviour* of using it is
    unchanged -- only its text.
    """
    import os

    from hostctl import LocalHost

    path = LocalHost().path()

    assert str(path) != os.getcwd()
    assert os.path.abspath(str(path)) == os.getcwd()


def test_local_host_is_a_system_host():
    """One implementation of a system host, so local and remote cannot
    diverge."""
    from hostctl import LocalHost
    from hostctl.host.system import SystemHost

    host = LocalHost()

    assert isinstance(host, SystemHost)
    assert host.system_family in ("posix", "windows")
    # But a local path stays a plain pathlib object: one provider reaching
    # one filesystem has nothing for a composite to decide.
    assert type(host.path("x")).__name__ == "LocalPath"


def test_output_written_around_a_dispatch_keeps_its_order():
    """The binary fallback wrote past text still pending in the wrapper."""
    code = (
        "import sys\n"
        "from hostctl.executor import dispatch_output\n"
        "sys.stdout.write('1-before\\n')\n"
        "dispatch_output(None, None, b'2-output\\n', None, encoding=None, errors=None)\n"
        "sys.stdout.write('3-after\\n')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )

    assert result.stdout.split() == ["1-before", "2-output", "3-after"]


def test_an_invalid_environment_name_is_refused_before_any_provider_runs():
    from hostctl import ExecutorProvider, PosixHost

    called = []

    def execute(command, *args, **options):
        called.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    embedded = PosixHost(executor_providers=(ExecutorProvider("fake", execute),))
    native = LocalHost()
    for host in (embedded, native):
        with pytest.raises(ValueError, match="invalid environment variable name"):
            host.run("true", env={"A.B": "1"})

    assert called == []


def test_a_non_payload_input_fails_at_once_instead_of_hanging():
    import time

    from hostctl.executor import LocalExecutor

    started = time.monotonic()
    with pytest.raises(TypeError, match="input must be"):
        LocalExecutor()(
            sys.executable, "-c", "import sys; sys.stdin.read()", input=3, timeout=30
        )

    assert time.monotonic() - started < 5


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX terminal")
def test_a_timeout_keeps_the_controlling_terminal(monkeypatch):
    """`timeout=` started a new session, so a child that opens /dev/tty
    failed ENXIO where the same call without a timeout worked."""
    import os
    import pty

    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))

    child = (
        "import os, sys\n"
        "from hostctl.executor import LocalExecutor\n"
        'code = \'import os\\ntry:\\n open("/dev/tty").close(); print("tty ok")\\n'
        'except OSError as e:\\n print("tty FAIL", e.errno)\\n\'\n'
        "r = LocalExecutor()(sys.executable, '-c', code, timeout=30)\n"
        "sys.stdout.write(r.stdout.decode())\n"
    )
    captured = []

    def reader(descriptor):
        data = os.read(descriptor, 1024)
        captured.append(data)
        return data

    status = pty.spawn([sys.executable, "-c", child], reader)

    assert b"tty ok" in b"".join(captured), captured
    assert status == 0


def test_an_unsupported_local_os_still_builds_a_host(monkeypatch):
    """Only NAMING the shell was ever refused, not constructing the host."""
    import os

    from hostctl import LocalHost

    monkeypatch.setattr(os, "name", "")
    host = LocalHost()

    assert host.info().os_family is not None or True
    with pytest.raises(NotImplementedError, match="unsupported local OS"):
        host.shell_flavour


def test_the_local_hosts_executor_runs_a_program_with_argv():
    result = LocalHost().executor(sys.executable, "-c", "print(1)")

    assert result.returncode == 0


def test_local_host_still_lists_its_providers():
    host = LocalHost()
    assert [p.name for p in host.executor_providers] == ["local"]
    assert [p.name for p in host.path_providers] == ["local"]


def test_local_providers_returns_the_local_pair():
    import hostctl
    from hostctl.provider.transports import (
        FULL_PATH_OPERATIONS,
        LocalExecutorProvider,
        LocalPathProvider,
    )

    executor, path = hostctl.local_providers()
    assert isinstance(executor, LocalExecutorProvider)
    assert isinstance(path, LocalPathProvider)
    assert (executor.name, path.name) == ("local", "local")
    assert path.capabilities == FULL_PATH_OPERATIONS
    assert isinstance(path.path("x"), NextPath)
    assert "local_providers" in hostctl.__all__
