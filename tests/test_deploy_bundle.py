"""What a deploy bundle carries, and what it must never silently not.

The failure these guard against is a bundle that builds and then cannot
import on the target, which no local step reports. The closure is built from
synthetic installed distributions (`_deploy_fakes.make_site`), so what is
installed in the test environment does not decide the outcome.
"""

from __future__ import annotations

import importlib
import io
import json
import logging
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("packaging")
pytest.importorskip("pathspec")

from _deploy_fakes import make_site  # noqa: E402
from hostctl import BundleError, MissingExtraError  # noqa: E402
from hostctl.deploy import SourceTree, bundle  # noqa: E402
from hostctl.deploy import _archive, _closure, _tree  # noqa: E402


@pytest.fixture
def site(tmp_path, monkeypatch):
    path = make_site(tmp_path)
    monkeypatch.syspath_prepend(str(path))
    importlib.invalidate_caches()
    return path


def _names(pyz: Path):
    data = pyz.read_bytes()
    with zipfile.ZipFile(io.BytesIO(data[data.index(b"PK\x03\x04") :])) as archive:
        return set(archive.namelist()), data


# -- the closure -----------------------------------------------------------


def test_the_closure_is_transitive_and_includes_the_root(site):
    found = _closure.requirements("hcd-app")

    assert {"hcd-app", "hcd-dep", "hcd-lib"} <= set(found)
    # Declared, not installed: what this environment cannot contribute.
    assert "hcd-absent" not in found


def test_a_real_installed_closure_follows_its_declared_dependencies():
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        distribution("hostctl")
    except PackageNotFoundError:
        pytest.skip("hostctl is imported from a checkout, not installed")
    assert {"hostctl", "pathapi", "netimps"} <= set(_closure.requirements("hostctl"))


def test_extras_are_excluded_unless_asked_for(site):
    assert "hcd-ssh" not in _closure.requirements("hcd-app")
    assert "hcd-ssh" in _closure.requirements("hcd-app", extras=("ssh",))


def test_a_requirements_own_extras_are_followed(site):
    """`hcd-lib[fast]` brings what only that extra of hcd-lib requires."""
    assert "hcd-speed" in _closure.requirements("hcd-app")


def test_the_roots_extras_are_not_applied_to_everything(site):
    """Asking hcd-app for `ssh` must not pull hcd-lib's own `ssh` extra."""
    with_ssh = _closure.requirements("hcd-app", extras=("ssh",))

    assert "hcd-paramiko" not in with_ssh


def test_markers_are_evaluated_against_the_target(site):
    windows = {"sys_platform": "win32", "os_name": "nt", "python_version": "3.11"}
    linux = {"sys_platform": "linux", "os_name": "posix", "python_version": "3.11"}

    assert "hcd-win" in _closure.requirements("hcd-app", environment=windows)
    assert "hcd-win" not in _closure.requirements("hcd-app", environment=linux)


def test_missing_on_ships_only_the_difference(site):
    linux = json.dumps({"__marker_environment__": {"sys_platform": "linux"}})
    missing = _closure.missing_on([linux, "hcd-dep", "hcd_lib"], "hcd-app")

    # `hcd_lib` and `hcd-lib` are one distribution.
    assert set(missing) == {"hcd-app", "hcd-speed"}


def test_skip_leaves_out_what_the_target_lacks(site):
    missing = _closure.missing_on([], "hcd-app", skip=["hcd-dep"])

    assert "hcd-dep" not in missing and "hcd-app" in missing


def test_the_probe_reads_its_environment_line_and_older_names_only_output():
    linux = {"sys_platform": "linux", "python_version": "3.11"}
    names, environment = _closure.parse_probe(
        json.dumps({"__marker_environment__": linux}) + "\nrequests\nidna\n"
    )
    assert names == ["requests", "idna"] and environment == linux

    assert _closure.parse_probe("requests\nidna\n") == (["requests", "idna"], {})


def test_the_probe_is_stdlib_only_and_reports_where_it_runs():
    """It runs on the target before anything is installed there."""
    result = subprocess.run(
        [sys.executable, "-I", "-"],
        input=_closure.PROBE_SOURCE,
        capture_output=True,
        text=True,
        check=True,
    )
    names, environment = _closure.parse_probe(result.stdout)

    assert "pip" in names or "pytest" in names
    assert not [name for name in names if name != name.lower() or "_" in name]
    assert environment["sys_platform"] == sys.platform
    assert environment["python_version"] == "%d.%d" % sys.version_info[:2]


# -- the zipapp and the tree -----------------------------------------------


def test_a_zipapp_carries_importable_packages_and_runs(site, tmp_path):
    pyz = bundle("hcd-app", tmp_path / "app.pyz")
    names, data = _names(pyz)

    assert data.startswith(b"#!/usr/bin/env python3\n")
    assert "__main__.py" in names
    for package in ("hcd_app", "hcd_lib", "hcd_speed"):
        assert f"{package}/__init__.py" in names
    assert "hcd_dep.py" in names
    # zipimport has no namespace packages: a subpackage needs its __init__.
    assert {"hcd_app/cmd/__init__.py", "hcd_app/cmd/call.py"} <= names
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    # RECORD's `../../bin/hcd-app` must not make the parent tree count.
    assert not [n for n in names if n.startswith(("bin/", "site/"))]

    ran = subprocess.run(
        [sys.executable, "-I", str(pyz), "x", "y"], capture_output=True, text=True
    )
    assert ran.returncode == 0, ran.stderr[-400:]
    assert ran.stdout.split() == ["hcd-app", "1.0", "x", "y"]


def test_the_shebang_names_the_python_given(site, tmp_path):
    absolute = bundle("hcd-app", tmp_path / "a.pyz", python="/opt/py/bin/python3")
    named = bundle("hcd-app", tmp_path / "b.pyz", python="python3.11")

    assert absolute.read_bytes().startswith(b"#!/opt/py/bin/python3\n")
    assert named.read_bytes().startswith(b"#!/usr/bin/env python3.11\n")


def test_a_tree_has_lib_and_a_launcher(site, tmp_path):
    root = bundle("hcd-app", tmp_path / "tree", mode="dir")

    assert (root / "lib" / "hcd_app" / "cmd" / "__init__.py").is_file()
    assert (root / "lib" / "hcd_dep.py").is_file()
    launcher = (root / "bin" / "hcd_app").read_text()
    assert launcher.startswith("#!/bin/sh\n")
    assert 'PYTHONPATH="$here/lib${PYTHONPATH:+:$PYTHONPATH}"' in launcher
    assert "exec python3 -m hcd_app" in launcher


def test_the_launcher_adds_no_empty_pythonpath_entry():
    """`dir:` with an empty PYTHONPATH would put the working directory on
    sys.path, ahead of the installed packages."""
    text = _archive.launcher("pkg", "python3", ["lib"]).decode()

    assert '$here/lib:$PYTHONPATH"' not in text
    assert "${PYTHONPATH:+:$PYTHONPATH}" in text


def test_the_launcher_quotes_the_interpreter():
    text = _archive.launcher("pkg", "/opt/my python/bin/python3", ["lib"]).decode()

    assert "exec '/opt/my python/bin/python3' -m pkg" in text


@pytest.mark.parametrize("package", ["", "bad name", "x;rm -rf /", "1abc"])
def test_a_package_must_be_an_import_name(site, tmp_path, package):
    with pytest.raises(ValueError, match="dotted import name"):
        bundle("hcd-app", tmp_path / "x.pyz", package=package)


def test_an_unknown_mode_is_refused(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        bundle("hcd-app", tmp_path / "x", mode="zip")


def test_a_data_only_distribution_is_refused(tmp_path, monkeypatch):
    readme = tmp_path / "README.md"
    readme.write_text("not a module")
    monkeypatch.setattr(_closure, "_top_level_paths", lambda dist: [readme])

    with pytest.raises(BundleError, match="data files only"):
        _closure.collect({"fake": object()})


def test_a_distribution_with_a_compiled_extension_is_refused(site):
    package = site / "hcd_native"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "_speed.so").write_bytes(b"\x7fELF")
    info = site / "hcd_native-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: hcd-native\n")
    (info / "RECORD").write_text("hcd_native/__init__.py,,\nhcd_native/_speed.so,,\n")
    importlib.invalidate_caches()

    with pytest.raises(BundleError, match="compiled extension.*skip"):
        bundle("hcd-native", site.parent / "n.pyz")


def test_an_editable_install_is_found_by_import(site, tmp_path, monkeypatch):
    """Its RECORD lists only a `.pth` shim; the package lives elsewhere."""
    elsewhere = tmp_path / "src"
    (elsewhere / "hcd_edit").mkdir(parents=True)
    (elsewhere / "hcd_edit" / "__init__.py").write_text("X = 1\n")
    monkeypatch.syspath_prepend(str(elsewhere))
    info = site / "hcd_edit-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: hcd-edit\n")
    (info / "RECORD").write_text("__editable__.hcd_edit-1.0.pth,,\n")
    (info / "top_level.txt").write_text("hcd_edit\n")
    importlib.invalidate_caches()

    contents = _closure.collect(_closure.requirements("hcd-edit"))

    assert ("hcd_edit/__init__.py", elsewhere / "hcd_edit" / "__init__.py") in contents


# -- installed metadata in the bundle --------------------------------------


def _fake_dist(name, version, metadata_text=None):
    class _Dist:
        def __init__(self):
            self.version = version
            self.metadata = {"Name": name, "Version": version}

        def read_text(self, filename):
            return metadata_text if filename == "METADATA" else None

    return _Dist()


def test_a_bundle_carries_a_dist_info_per_distribution():
    entries = dict(
        _closure.metadata_entries(
            {
                "demo": _fake_dist(
                    "demo-dist",
                    "1.2.3",
                    "Metadata-Version: 2.1\nName: demo-dist\nVersion: 1.2.3\n"
                    "Summary: real\n",
                ),
                "other": _fake_dist("other", "9.0"),
            }
        )
    )

    assert b"Summary: real" in entries["demo_dist-1.2.3.dist-info/METADATA"]
    assert entries["other-9.0.dist-info/METADATA"] == (
        b"Metadata-Version: 2.1\nName: other\nVersion: 9.0\n"
    )
    assert entries["demo_dist-1.2.3.dist-info/INSTALLER"] == b"hostctl-deploy\n"


def test_a_distribution_without_a_version_is_skipped():
    assert _closure.metadata_entries({"x": _fake_dist("x", "")}) == []


def test_a_tree_bundle_carries_no_metadata(tmp_path):
    """A working tree has no installed metadata, and inventing a version
    would be a lie."""
    (tmp_path / "repo" / "pkg").mkdir(parents=True)
    (tmp_path / "repo" / "pkg" / "__init__.py").write_text("X = 1\n")

    root = bundle(
        SourceTree(tmp_path / "repo"), tmp_path / "t", mode="dir", package="pkg"
    )

    assert not list(root.rglob("*.dist-info"))


# -- reproducible payloads -------------------------------------------------


def test_payloads_depend_only_on_their_contents(site, tmp_path):
    """The digest recorded on the target skips an unchanged deploy only if a
    rebuild of the same inputs is byte-identical, whatever the file times."""
    first = bundle("hcd-app", tmp_path / "a.pyz").read_bytes()
    for path in site.rglob("*"):
        os.utime(path, (1_700_000_000, 1_700_000_000))
    second = bundle("hcd-app", tmp_path / "b.pyz").read_bytes()
    assert first == second
    with zipfile.ZipFile(io.BytesIO(first[first.index(b"PK\x03\x04") :])) as archive:
        assert {info.date_time[:3] for info in archive.infolist()} == {(1980, 1, 1)}

    # The same top-level name in two staging directories, as deploy names
    # the tree after the remote target.
    one = bundle("hcd-app", tmp_path / "s1" / "app", mode="dir")
    digest, payload = _archive.tar_digest(one)
    for path in site.rglob("*"):
        os.utime(path, (1_600_000_000, 1_600_000_000))
    two = bundle("hcd-app", tmp_path / "s2" / "app", mode="dir")
    assert _archive.tar_digest(two)[0] == digest
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        members = tar.getmembers()
    assert {member.mtime for member in members} == {_archive.EPOCH}
    assert {(member.uid, member.gid) for member in members} == {(0, 0)}
    modes = {member.name: member.mode for member in members}
    assert modes["app/bin/hcd_app"] == 0o755
    assert modes["app/lib/hcd_dep.py"] == 0o644


# -- source trees ----------------------------------------------------------


def _arcnames(tree):
    return {name for name, _ in tree.files()}


def _repo(tmp_path):
    """A tree shaped like the ones that broke: a nested ignore file, a nested
    checkout, and (where the OS allows) a symlink pointing outside."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.env").write_text("TOKEN=hunter2\n")
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / ".gitignore").write_text("*.log\n")
    (root / "src" / ".gitignore").write_text("generated/\n")
    (root / "src" / "generated").mkdir()
    (root / "src" / "generated" / "big.py").write_text("# generated\n")
    (root / "src" / "pkg" / "__init__.py").write_text("x = 1\n")
    (root / "keep.py").write_text("y = 2\n")
    (root / "noisy.log").write_text("noise\n")
    (root / "vendor" / "dep" / ".git").mkdir(parents=True)
    (root / "vendor" / "dep" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "vendor" / "dep" / "mod.py").write_text("z = 3\n")
    return root, outside


def test_a_tree_ships_what_a_clone_would(tmp_path):
    root, _ = _repo(tmp_path)
    names = _arcnames(SourceTree(root))

    assert {
        "repo/keep.py",
        "repo/src/pkg/__init__.py",
        "repo/vendor/dep/mod.py",
    } <= names
    assert "repo/noisy.log" not in names
    assert not [n for n in names if "generated" in n]
    assert not [n for n in names if ".git/" in n]


def test_a_nested_ignore_file_is_anchored_to_its_directory(tmp_path):
    root, _ = _repo(tmp_path)
    (root / "generated").mkdir()
    (root / "generated" / "keepme.py").write_text("w = 4\n")
    names = _arcnames(SourceTree(root))

    assert "repo/generated/keepme.py" in names
    assert "repo/src/generated/big.py" not in names


def test_a_symlink_out_of_the_tree_is_not_followed(tmp_path):
    root, outside = _repo(tmp_path)
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create a symlink")

    names = _arcnames(SourceTree(root))

    assert not [n for n in names if "secret.env" in n or n.startswith("repo/link")]


def test_a_negation_re_includes_a_file(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text("*.log\n!keep.log\n")
    (root / "a.log").write_text("x")
    (root / "keep.log").write_text("x")
    names = _arcnames(SourceTree(root))

    assert "repo/keep.log" in names and "repo/a.log" not in names


def test_an_ignored_directory_is_pruned(tmp_path):
    root = tmp_path / "repo"
    (root / ".venv3" / "lib").mkdir(parents=True)
    (root / ".gitignore").write_text(".venv*/\n")
    (root / ".venv3" / "lib" / "x.py").write_text("y")
    (root / "src").mkdir()
    (root / "src" / "ok.py").write_text("z")

    assert _arcnames(SourceTree(root)) == {"repo/.gitignore", "repo/src/ok.py"}


def test_ignore_files_selects_which_are_read(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text("a.txt\n")
    (root / ".bundleignore").write_text("b.txt\n")
    for name in ("a.txt", "b.txt", "c.txt"):
        (root / name).write_text("x")

    only = _arcnames(SourceTree(root, ignore_files=[".gitignore"]))
    assert "repo/a.txt" not in only and "repo/b.txt" in only

    every = _arcnames(SourceTree(root))
    assert "repo/a.txt" not in every and "repo/b.txt" not in every
    assert "repo/c.txt" in every


def test_a_named_ignore_file_that_is_absent_is_an_error(tmp_path):
    """Silently skipping it would disable the filtering asked for, and a
    gitignored secret would ship."""
    root, _ = _repo(tmp_path)
    with pytest.raises(BundleError, match="nothing would be filtered"):
        SourceTree(root, ignore_files=["  .bundleignore"]).files()
    with pytest.raises(BundleError, match="not found"):
        SourceTree(root, ignore_files=[".nope"]).files()
    # The defaults stay forgiving: a tree need not have all three.
    assert SourceTree(root).files()


def test_ignore_patterns_layer_after_the_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text("*.txt\n")
    (root / "secret.cfg").write_text("x")
    (root / "kept.txt").write_text("x")

    names = _arcnames(SourceTree(root, ignore_patterns=["secret.cfg", "!kept.txt"]))

    assert names == {"repo/.gitignore", "repo/kept.txt"}


def test_the_arcname_prefix_can_be_named(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "f.txt").write_text("x")

    names = {n for n, _ in _tree.collect_tree(tmp_path / "repo", prefix="myapp")}

    assert names == {"myapp/f.txt"}


def test_a_tree_root_must_be_a_directory(tmp_path):
    (tmp_path / "f.txt").write_text("x")

    with pytest.raises(BundleError, match="not a directory"):
        SourceTree(tmp_path / "f.txt").files()


def test_a_tree_ships_only_as_a_directory(tmp_path):
    with pytest.raises(BundleError, match="only as mode='dir'"):
        bundle(SourceTree(tmp_path), tmp_path / "x.pyz", package="pkg")


def test_a_tree_needs_its_package_named(tmp_path):
    with pytest.raises(TypeError, match="package="):
        bundle(SourceTree(tmp_path), tmp_path / "x", mode="dir")


def test_a_src_layout_tree_gets_a_launcher_that_reaches_it(tmp_path):
    (tmp_path / "repo" / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "repo" / "src" / "pkg" / "__init__.py").write_text("X = 1\n")

    root = bundle(
        SourceTree(tmp_path / "repo"), tmp_path / "t", mode="dir", package="pkg"
    )

    assert (root / "lib" / "repo" / "src" / "pkg" / "__init__.py").is_file()
    assert '"$here/lib/repo/src${PYTHONPATH' in (root / "bin" / "pkg").read_text()


@pytest.mark.parametrize(
    "layout, expected",
    [
        ({"src/pkg/__init__.py"}, ["src"]),
        ({"pkg/__init__.py"}, [""]),
        ({"lib/pkg.py"}, ["lib"]),
        ({"src/pkg/__init__.py", "lib/pkg/__init__.py"}, ["src", "lib"]),
        (set(), []),
    ],
)
def test_the_import_roots_are_detected(tmp_path, layout, expected):
    for name in layout:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("")

    assert _tree.detect_pythonpath(tmp_path, "pkg") == expected


def test_named_import_roots_win_and_a_missing_package_is_an_error(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    name = tmp_path.resolve().name

    assert SourceTree(tmp_path, pythonpath=["custom"])._import_roots("pkg") == [
        f"{name}/custom"
    ]
    with pytest.raises(BundleError, match="could not find an importable 'nope'"):
        SourceTree(tmp_path)._import_roots("nope")


# -- declared requirements of a tree ---------------------------------------


def _pyproject(tmp_path, text):
    (tmp_path / "pyproject.toml").write_text(text)
    return SourceTree(tmp_path)


def test_requirements_come_from_pyproject(tmp_path):
    tree = _pyproject(
        tmp_path,
        '[project]\nname = "x"\ndependencies = ["requests>=2.0", "click"]\n'
        '[project.optional-dependencies]\nssh = ["asyncssh"]\n',
    )

    assert set(tree.requirements()) == {"requests", "click"}
    assert "asyncssh" in tree.requirements(extras=("ssh",))


def test_requirements_fall_back_to_requirements_txt(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "requests>=2.0\n# a comment\n\n-e ./local\nclick\n"
    )

    assert set(SourceTree(tmp_path).requirements()) == {"requests", "click"}


def test_pyproject_wins_over_requirements_txt(tmp_path):
    (tmp_path / "requirements.txt").write_text("fromtxt\n")
    tree = _pyproject(tmp_path, '[project]\nname = "x"\ndependencies = ["fromtoml"]\n')

    assert tree.requirements() == ["fromtoml"]


def test_a_tree_declaring_nothing_is_an_error(tmp_path):
    with pytest.raises(BundleError, match="pyproject.toml or requirements.txt"):
        SourceTree(tmp_path).requirements()


def test_include_and_exclude_take_names_and_extras(tmp_path):
    tree = _pyproject(
        tmp_path,
        '[project]\nname = "x"\ndependencies = ["requests", "asyncssh"]\n'
        '[project.optional-dependencies]\nssh = ["asyncssh"]\nfast = ["ujson"]\n',
    )

    assert "ujson" in tree.requirements(include=["[fast]"])
    assert "extra-dep" in tree.requirements(include=["extra-dep"])
    # An excluded extra removes what it declares, though it was never asked for.
    assert tree.requirements(exclude=["[ssh]"]) == ["requests"]
    # An explicit exclude beats an equally explicit include.
    assert "requests" not in tree.requirements(
        include=["requests"], exclude=["requests"]
    )


def test_requirements_are_deduplicated(tmp_path):
    tree = _pyproject(
        tmp_path,
        '[project]\nname = "x"\ndependencies = ["requests"]\n'
        '[project.optional-dependencies]\nssh = ["requests"]\n',
    )

    assert tree.requirements(extras=("ssh",)).count("requests") == 1


def test_a_requirements_file_survives_its_own_oddities(tmp_path, caplog):
    """The list is advisory: one unparseable line must not fail a deploy."""
    (tmp_path / "requirements.txt").write_text(
        "# a comment\n"
        "requests>=2\n"
        "duho  # inline comment\n"
        "pathlib_next[uri]; python_version >= '3.9'\n"
        "some-\\\n"
        "package==1.0\n"
        "-r other.txt\n"
        "!!! not a requirement !!!\n"
    )
    with caplog.at_level(logging.WARNING, logger="hostctl.deploy._tree"):
        names = SourceTree(tmp_path).requirements()

    assert {"requests", "duho", "pathlib_next", "some-package"} <= set(names)
    assert "ignoring unparseable requirement" in caplog.text


# -- the extra, missing ----------------------------------------------------


def _block(monkeypatch, *names):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.split(".")[0] in names:
            raise ImportError(f"{name} blocked by the test")
        return real_import(name, *args, **kwargs)

    for module in list(sys.modules):
        if module.split(".")[0] in names:
            monkeypatch.delitem(sys.modules, module)
    monkeypatch.setattr(builtins, "__import__", blocked)


def test_a_tree_without_pathspec_names_the_extra(tmp_path, monkeypatch):
    (tmp_path / "f.txt").write_text("x")
    _block(monkeypatch, "pathspec")

    with pytest.raises(MissingExtraError, match=r"hostctl\[deploy\]"):
        SourceTree(tmp_path).files()


def test_a_pyproject_without_a_toml_parser_names_the_extra(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = []\n")
    _block(monkeypatch, "tomllib", "tomli")

    with pytest.raises(MissingExtraError, match=r"'deploy' extra"):
        SourceTree(tmp_path).requirements()


def test_importing_the_module_imports_no_optional_dependency():
    code = (
        "import sys, hostctl.deploy;"
        "print(sorted({'packaging', 'pathspec', 'tomli'} & set(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "src")),
    )

    assert result.stdout.strip() == "[]"
