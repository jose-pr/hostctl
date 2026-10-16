"""`hostctl deploy URI SOURCE PATH [-- ARG...]`, over the sandboxed POSIX host."""

from __future__ import annotations

import importlib
import re

import duho.mcp as duho_mcp
import pytest

pytest.importorskip("packaging")
pytest.importorskip("pathspec")
pytest.importorskip("sftppath")

from _deploy_fakes import make_site, sandbox_host  # noqa: E402
from hostctl.cli import main  # noqa: E402
from hostctl.cli._root import Hostctl  # noqa: E402

LINUX = '{"__marker_environment__": {"sys_platform": "linux"}}'


@pytest.fixture
def site(tmp_path, monkeypatch):
    path = make_site(tmp_path)
    monkeypatch.syspath_prepend(str(path))
    importlib.invalidate_caches()
    return path


@pytest.fixture
def target(monkeypatch):
    """Every URI the command line opens is the one sandboxed host."""
    host, programs = sandbox_host()
    programs.probe_reply = [LINUX]
    opened = []

    def open_host(uri, **credentials):
        opened.append(uri)
        return host

    monkeypatch.setattr("hostctl.cli._common.Host", open_host)
    try:
        yield host, programs, opened
    finally:
        host.close()
        host._sandbox_cleanup()


@pytest.fixture
def repo(tmp_path):
    """A src-layout tree whose program exits with the status it is given."""
    root = tmp_path / "repo"
    (root / "src" / "tool").mkdir(parents=True)
    (root / "src" / "tool" / "__init__.py").write_text("")
    (root / "src" / "tool" / "__main__.py").write_text(
        "import sys\nprint('tool', *sys.argv[1:])\nsys.exit(int(sys.argv[1]))\n"
    )
    (root / "notes.log").write_text("x")
    return root


def test_a_deploy_prints_one_line_and_a_repeat_says_current(site, target, capsys):
    _, programs, opened = target

    assert main(["deploy", "ssh://nas", "hcd-app", "/opt/app.pyz"]) == 0
    first = capsys.readouterr().out
    assert re.fullmatch(r"deployed /opt/app\.pyz [0-9a-f]{12}\n", first)
    assert opened == ["ssh://nas"]

    assert main(["deploy", "ssh://nas", "hcd-app", "/opt/app.pyz"]) == 0
    assert capsys.readouterr().out == first.replace("deployed", "current")
    assert programs.local("/opt/app.pyz").is_file()


def test_the_arguments_after_the_separator_run_it(site, target, capsysbinary):
    code = main(
        ["deploy", "ssh://nas", "hcd-app", "/opt/app", "--mode", "dir", "--", "a"]
    )

    assert code == 0
    assert capsysbinary.readouterr().out.split() == [b"hcd-app", b"1.0", b"a"]


def test_a_tree_runs_with_its_own_exit_status(repo, target, capsysbinary):
    _, programs, _ = target
    argv = ["deploy", "ssh://nas", str(repo), "/opt/tool", "--tree", "--mode", "dir"]
    argv += ["--package", "tool", "--ignore-pattern", "*.log", "--", "7"]

    assert main(argv) == 7

    assert capsysbinary.readouterr().out.split() == [b"tool", b"7"]
    assert not list(programs.local("/opt/tool").rglob("notes.log"))


def test_options_reach_the_library(target, monkeypatch, tmp_path):
    seen = {}

    def fake(host, source, path, **options):
        seen.update(options, source=source, path=path)
        raise ValueError("stop here")

    monkeypatch.setattr("hostctl.deploy.deploy", fake)
    argv = ["deploy", "ssh://nas", "app", "/opt/app", "--extra", "ssh"]
    argv += [
        "--extra",
        "fast",
        "--skip",
        "certifi",
        "--force",
        "--python",
        "python3.12",
    ]

    assert main(argv) == 125
    assert seen == {
        "source": "app",
        "path": "/opt/app",
        "mode": "pyz",
        "package": None,
        "extras": ["ssh", "fast"],
        "skip": ["certifi"],
        "force": True,
        "python": "python3.12",
    }

    argv = ["deploy", "ssh://nas", str(tmp_path), "/opt/t", "--tree", "--package", "p"]
    argv += ["--ignore-file", ".gitignore", "--pythonpath", "src"]
    main(argv)
    tree = seen["source"]
    assert (tree.root, tree.ignore_files, tree.pythonpath) == (
        tmp_path,
        (".gitignore",),
        ("src",),
    )


def test_a_foreign_path_exits_125_and_is_left_alone(site, target, capsys):
    _, programs, _ = target
    foreign = programs.local("/opt/app.pyz")
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"theirs")

    assert main(["deploy", "ssh://nas", "hcd-app", "/opt/app.pyz", "--force"]) == 125

    assert capsys.readouterr().err.startswith(
        "hostctl: refusing to replace /opt/app.pyz"
    )
    assert foreign.read_bytes() == b"theirs"


def test_a_bundle_that_cannot_be_built_exits_125(repo, target, capsys):
    _, programs, _ = target

    code = main(
        ["deploy", "ssh://nas", str(repo), "/opt/t", "--tree", "--package", "tool"]
    )

    assert code == 125
    assert "only as mode='dir'" in capsys.readouterr().err
    assert programs.ran == []


def test_a_failed_step_exits_125_naming_what_it_said(site, target, capsys):
    _, programs, _ = target
    programs.fail[("chmod",)] = (b"", b"chmod: changing permissions: Read-only\n", 1)

    assert main(["deploy", "ssh://nas", "hcd-app", "/opt/app.pyz"]) == 125

    err = capsys.readouterr().err
    assert err.startswith("hostctl: ") and len(err.splitlines()) == 1
    assert err.rstrip().endswith("chmod: changing permissions: Read-only")


def test_deploy_is_an_mcp_tool_and_runs_as_one(site, target):
    tools = {tool["name"]: tool for tool in duho_mcp.describe_tools(Hostctl)}
    properties = tools["hostctl.deploy"]["inputSchema"]["properties"]
    assert {"uri", "source", "path", "mode", "tree", "--"} <= set(properties)

    result = duho_mcp.call_tool(
        Hostctl,
        "hostctl.deploy",
        {"uri": "ssh://nas", "source": "hcd-app", "path": "/opt/app", "mode": "dir"},
    )

    assert not result.get("isError"), result
    assert result["content"][0]["text"].startswith("deployed /opt/app ")
