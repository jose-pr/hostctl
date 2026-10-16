"""What only the in-process asyncssh leg can host.

The SFTP fakes report `supports_tree=False` and the fake channel is a local
pipe, so sftppath's native tree operations, a real login shell and a real
SSH signal are reachable only through `ssh-inproc`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

pytest.importorskip("asyncssh")

from sftppath import AsyncSSHBackend  # noqa: E402

from hostctl.shell._grammar import Exec  # noqa: E402

from .providers import (  # noqa: E402
    conformance_path,
    deterministic_providers,
    provider_context,
)

_LEG = next(p for p in deterministic_providers() if p.name == "ssh-inproc")


def _spy(monkeypatch, name):
    calls = []
    original = getattr(AsyncSSHBackend, name)

    def spy(self, *args, **kwargs):
        calls.append(name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSSHBackend, name, spy)
    return calls


def _tree(root):
    root.mkdir(parents=True)
    (root / "a.txt").write_bytes(b"alpha")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_bytes(b"beta")


def test_recursive_rm_takes_the_native_tree_walk(tmp_path, monkeypatch):
    calls = _spy(monkeypatch, "tree_rm")
    with provider_context(_LEG) as host:
        root = conformance_path(host, _LEG, tmp_path, "rm-tree")
        _tree(root)

        root.rm(recursive=True)

        assert not root.exists()
    assert calls == ["tree_rm"]


def test_tree_copy_takes_the_native_fan_out(tmp_path, monkeypatch):
    calls = _spy(monkeypatch, "tree_copy")
    with provider_context(_LEG) as host:
        source = conformance_path(host, _LEG, tmp_path, "copy-source")
        target = conformance_path(host, _LEG, tmp_path, "copy-target")
        _tree(source)

        source.copy(target, recursive=True)

        assert (target / "a.txt").read_bytes() == b"alpha"
        assert (target / "sub" / "b.txt").read_bytes() == b"beta"
    assert calls == ["tree_copy"]


def test_spawn_round_trips_through_the_login_shell():
    echo = "import sys; sys.stdout.write(sys.stdin.read().upper())"
    with provider_context(_LEG) as host:
        line = host.shell_flavour.structured_command((sys.executable, "-c", echo))
        process = host.spawn(line)
        try:
            process.write(b"through the login shell")
            process.send_eof()
            assert process.wait(timeout=60) == 0
            assert process.read() == b"THROUGH THE LOGIN SHELL"
        finally:
            process.close()


def test_run_cwd_and_env_reach_the_child(tmp_path):
    code = "import os; print(os.getcwd()); print(os.environ['HOSTCTL_INPROC'])"
    with provider_context(_LEG) as host:
        result = host.run(
            Exec(sys.executable, "-c", code),
            cwd=tmp_path,
            env={"HOSTCTL_INPROC": "seen"},
            text=True,
        )
    cwd, value = result.stdout.splitlines()[:2]
    assert os.path.normcase(cwd) == os.path.normcase(str(tmp_path))
    assert value == "seen"


def test_a_timed_out_command_ends_on_the_ssh_signal():
    """The server ends the child on TERM, so the result is not orphaned."""
    with provider_context(_LEG) as host:
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired) as raised:
            host.run(
                Exec(sys.executable, "-c", "import time; time.sleep(30)"),
                timeout=1,
            )
        elapsed = time.monotonic() - started
    assert raised.value.orphaned is False
    assert elapsed < 10
