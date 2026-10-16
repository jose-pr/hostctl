"""`deploy()` against a target: probe, digest, atomic swap, refusals.

The target is a real `PosixHost` over a sandbox (`_deploy_fakes`), whose
programs behave as GNU coreutils do, so each test asserts what the target
holds afterwards. On a POSIX machine the same flow also runs against
`local:` and the in-process SSH server, with the real tools.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("packaging")
pytest.importorskip("pathspec")
pytest.importorskip("sftppath")

from _deploy_fakes import make_site, sandbox_host  # noqa: E402
from hostctl import BundleError, Exec  # noqa: E402
from hostctl.deploy import SourceTree, deploy  # noqa: E402

LINUX = json.dumps({"__marker_environment__": {"sys_platform": "linux"}})


@pytest.fixture
def site(tmp_path, monkeypatch):
    path = make_site(tmp_path)
    monkeypatch.syspath_prepend(str(path))
    importlib.invalidate_caches()
    return path


@pytest.fixture
def target():
    host, programs = sandbox_host()
    programs.probe_reply = [LINUX, "pip"]
    try:
        yield host, programs
    finally:
        host.close()
        host._sandbox_cleanup()


def _local(programs, remote):
    return programs.local(remote)


def _writes(programs):
    return [argv for argv in programs.ran if argv[0] in ("mv", "rm", "tar", "chmod")]


# -- a zipapp --------------------------------------------------------------


def test_a_zipapp_is_installed_and_runs(site, target):
    host, programs = target
    programs.probe_reply = None  # the real probe, run by this interpreter

    done = deploy(host, "hcd-app", "/opt/app.pyz")

    assert done.written and done.path == "/opt/app.pyz"
    assert done.command == ("python3", "/opt/app.pyz")
    assert _local(programs, "/opt/app.pyz.digest").read_bytes() == (
        done.digest.encode() + b"\n"
    )
    assert not _local(programs, "/opt/app.pyz.new").exists()
    assert ("mv", "-f", "--", "/opt/app.pyz.new", "/opt/app.pyz") in programs.ran
    ran = host.run(Exec(*done.command, "hello"), text=True)
    assert ran.stdout.split() == ["hcd-app", "1.0", "hello"]


def test_only_what_the_target_lacks_ships(site, target):
    host, programs = target
    programs.probe_reply = [LINUX, "hcd-dep", "hcd_lib", "hcd-speed"]

    deploy(host, "hcd-app", "/opt/app.pyz")

    data = _local(programs, "/opt/app.pyz").read_bytes()
    assert b"hcd_app/__init__.py" in data
    assert b"hcd_dep.py" not in data and b"hcd_lib/" not in data


def test_markers_are_the_targets(site, target):
    host, programs = target
    windows = json.dumps({"__marker_environment__": {"sys_platform": "win32"}})
    programs.probe_reply = [windows]

    deploy(host, "hcd-app", "/opt/app.pyz")

    assert b"hcd_win/__init__.py" in _local(programs, "/opt/app.pyz").read_bytes()


def test_the_probe_runs_the_python_named(site, target):
    host, programs = target

    done = deploy(host, "hcd-app", "/opt/app.pyz", python="/usr/local/bin/python3.12")

    assert programs.ran[0] == ("/usr/local/bin/python3.12", "-")
    assert done.command[0] == "/usr/local/bin/python3.12"
    assert (
        _local(programs, "/opt/app.pyz")
        .read_bytes()
        .startswith(b"#!/usr/local/bin/python3.12\n")
    )


def test_an_unchanged_zipapp_is_not_written_again_unless_forced(site, target):
    host, programs = target
    first = deploy(host, "hcd-app", "/opt/app.pyz")
    programs.ran.clear()

    again = deploy(host, "hcd-app", "/opt/app.pyz")
    assert not again.written and again.digest == first.digest
    assert _writes(programs) == []

    forced = deploy(host, "hcd-app", "/opt/app.pyz", force=True)
    assert forced.written and _writes(programs)


def test_a_zipapp_never_replaces_a_file_it_did_not_write(site, target):
    host, programs = target
    foreign = _local(programs, "/opt/tool.pyz")
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"somebody else's")

    with pytest.raises(FileExistsError, match="not a previous deploy"):
        deploy(host, "hcd-app", "/opt/tool.pyz", force=True)

    assert foreign.read_bytes() == b"somebody else's"
    assert _writes(programs) == []


def test_the_parent_directory_is_created(site, target):
    host, programs = target

    deploy(host, "hcd-app", "/var/db/deep/app.pyz")

    assert _local(programs, "/var/db/deep/app.pyz").is_file()


# -- a tree ----------------------------------------------------------------

T = "/mnt/tank/app"


def test_a_tree_is_installed_with_its_marker_and_runs(site, target):
    host, programs = target

    done = deploy(host, "hcd-app", T + "/", mode="dir")

    assert done.path == T and done.command == (f"{T}/bin/hcd_app",)
    root = _local(programs, T)
    assert (root / "lib" / "hcd_app" / "__init__.py").is_file()
    assert (root / ".digest").read_bytes() == done.digest.encode() + b"\n"
    assert os.access(root / "bin" / "hcd_app", os.X_OK) or os.name == "nt"
    leftovers = [T + ".new", T + ".old", T + ".incoming.tar"]
    assert not [name for name in leftovers if _local(programs, name).exists()]
    ran = host.run(Exec(*done.command, "x"), text=True)
    assert ran.stdout.split() == ["hcd-app", "1.0", "x"]


def test_a_tree_replaces_a_previous_deploy(site, target):
    host, programs = target
    root = _local(programs, T)
    (root / "bin").mkdir(parents=True)
    (root / ".digest").write_bytes(b"d0\n")
    (root / "stale").write_bytes(b"x")

    assert deploy(host, "hcd-app", T, mode="dir").written

    assert not (root / "stale").exists()
    assert (root / "bin" / "hcd_app").is_file()
    assert not _local(programs, T + ".old").exists()


def test_a_tree_never_replaces_a_directory_it_did_not_create(site, target):
    host, programs = target
    root = _local(programs, T)
    (root / "sub").mkdir(parents=True)
    (root / "notes.txt").write_bytes(b"mine")
    (root / "sub" / "data.db").write_bytes(b"mine too")

    with pytest.raises(FileExistsError, match="not a previous deploy"):
        deploy(host, "hcd-app", T, mode="dir", force=True)

    assert (root / "notes.txt").read_bytes() == b"mine"
    assert (root / "sub" / "data.db").read_bytes() == b"mine too"
    assert _writes(programs) == []


def test_an_interrupted_swap_is_restored_not_deleted(site, target):
    """Killed between the two renames: `.old` holds the only good copy."""
    host, programs = target
    first = deploy(host, "hcd-app", T, mode="dir")
    os.replace(_local(programs, T), _local(programs, T + ".old"))
    programs.ran.clear()

    again = deploy(host, "hcd-app", T, mode="dir")

    assert not again.written and again.digest == first.digest
    assert (_local(programs, T) / "bin" / "hcd_app").is_file()
    assert _writes(programs) == [("mv", "--", T + ".old", T)]


def test_an_unchanged_tree_uploads_nothing(site, target):
    host, programs = target
    deploy(host, "hcd-app", T, mode="dir")
    programs.ran.clear()

    assert not deploy(host, "hcd-app", T, mode="dir").written

    assert _writes(programs) == []
    assert not _local(programs, T + ".incoming.tar").exists()


def test_a_failed_step_raises_and_keeps_the_previous_tree(site, target):
    host, programs = target
    deploy(host, "hcd-app", T, mode="dir")
    (_local(programs, T) / "marker").write_bytes(b"old tree")
    programs.fail[("tar",)] = (b"", b"tar: write error\n", 2)

    with pytest.raises(subprocess.CalledProcessError):
        deploy(host, "hcd-app", T, mode="dir", force=True)

    assert (_local(programs, T) / "marker").read_bytes() == b"old tree"


def test_a_source_tree_is_installed_with_its_layout(target, tmp_path):
    host, programs = target
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text("")
    (repo / "src" / "pkg" / "__main__.py").write_text(
        "import sys; print('tree', *sys.argv[1:])\n"
    )

    done = deploy(host, SourceTree(repo), T, mode="dir", package="pkg")

    # No probe: a tree vendors nothing, so the target is not asked.
    assert programs.ran[0][0] != "python3"
    launcher = (_local(programs, T) / "bin" / "pkg").read_text()
    assert "$here/lib/repo/src" in launcher
    assert host.run(Exec(*done.command, "ok"), text=True).stdout.split() == [
        "tree",
        "ok",
    ]


# -- refused before anything is sent ---------------------------------------


def test_a_tree_as_a_zipapp_never_touches_the_host(target, tmp_path):
    host, programs = target

    with pytest.raises(BundleError, match="only as mode='dir'"):
        deploy(host, SourceTree(tmp_path), "/opt/x.pyz", package="pkg")

    assert programs.ran == []


def test_a_relative_path_is_refused(site, target):
    host, programs = target

    with pytest.raises(ValueError, match="absolute"):
        deploy(host, "hcd-app", "opt/app.pyz")

    assert programs.ran == []


def test_a_windows_host_is_refused():
    from hostctl import ExecutorProvider, WindowsHost

    from _deploy_fakes import SandboxPrograms

    programs = SandboxPrograms(sandbox=None)
    host = WindowsHost(executor_providers=(ExecutorProvider("x", programs),))

    with pytest.raises(NotImplementedError, match="POSIX target"):
        deploy(host, "hostctl", "/opt/app.pyz")

    assert programs.ran == []


# -- live legs: the real tools ---------------------------------------------

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="the install steps are POSIX programs"
)


@posix_only
@pytest.mark.parametrize("mode", ["pyz", "dir"])
def test_a_local_posix_host_installs_and_runs_it(site, tmp_path, mode):
    from hostctl import Host

    path = str(tmp_path / "opt" / ("app.pyz" if mode == "pyz" else "app"))
    with Host("local:") as host:
        done = deploy(host, "hcd-app", path, mode=mode, python=sys.executable)
        again = deploy(host, "hcd-app", path, mode=mode, python=sys.executable)
        ran = host.run(Exec(*done.command, "live"), text=True)

    assert done.written and not again.written
    assert ran.stdout.split() == ["hcd-app", "1.0", "live"]


@posix_only
def test_an_ssh_host_installs_and_runs_it(site, tmp_path):
    pytest.importorskip("asyncssh")
    from conformance.inprocess_ssh import InProcessSSHServer, login_shell_handler

    path = str(tmp_path / "opt" / "app")
    with InProcessSSHServer(login_shell_handler, sftp_root="/") as server:
        with server.config().open() as host:
            done = deploy(host, "hcd-app", path, mode="dir", python=sys.executable)
            ran = host.run(Exec(*done.command, "ssh"), text=True)

    assert Path(path, "bin", "hcd_app").is_file()
    assert ran.stdout.split() == ["hcd-app", "1.0", "ssh"]
