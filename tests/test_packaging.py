"""What the built artifacts actually contain.

The shipped API header (`src/hostctl/AGENTS.md`) must be in the wheel, and
nothing matching `*.local.*` ever may be: an `AGENTS.local.md` sits in the
same directory under a nearly identical name, carries local paths, hostnames
and sometimes credentials, and a published artifact cannot be recalled.

The rule has two teeth -- `.gitignore` and the build manifest -- and neither
is automatic. Measured with both removed, `hostctl/AGENTS.local.md` shipped
inside the wheel.
"""

from __future__ import annotations

import ast
import inspect
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    """A copy of the tracked tree to build from, with the unshared files an
    exclusion exists for planted in it.

    Building from the checkout itself would write those files into `src/`,
    and an interrupted run would leave them there. The tracked files are
    copied with their working-tree contents, so the build tests the edit
    under test and not the last commit.
    """
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(ROOT),
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        pytest.skip(f"the tracked file list is unavailable: {error}")
    copy = tmp_path_factory.mktemp("tree")
    for name in listed.decode("utf-8").split("\0"):
        source = ROOT / name
        # No .gitignore: the build manifest must exclude the planted files on
        # its own, not lean on the ignore file hatchling also honours.
        if name and name != ".gitignore" and source.is_file():
            target = copy / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    for planted in (
        "src/hostctl/AGENTS.local.md",
        "CLAUDE.md",
        "src/hostctl/CLAUDE.md",
    ):
        (copy / planted).write_text(
            "machine-specific notes" + chr(10), encoding="utf-8"
        )
    return copy


@pytest.fixture(scope="module")
def wheel(tmp_path_factory, tree):
    pytest.importorskip("hatchling", reason="the build backend is not installed")
    pytest.importorskip("build", reason="the build frontend is not installed")
    output = tmp_path_factory.mktemp("dist")
    # `-n`: no isolation, so this needs no network and uses the backend that
    # is already installed.
    built = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "-n", "-o", str(output)],
        cwd=str(tree),
        capture_output=True,
        text=True,
    )
    if built.returncode != 0:
        pytest.skip(
            f"wheel build unavailable: {built.stdout[-400:]}{built.stderr[-400:]}"
        )
    wheels = sorted(output.glob("*.whl"))
    assert wheels, built.stdout
    return wheels[-1]


def test_the_wheel_ships_the_api_header(wheel):
    names = zipfile.ZipFile(wheel).namelist()

    assert "hostctl/AGENTS.md" in names
    assert "hostctl/README.md" in names


def test_the_wheel_declares_the_deploy_extra(wheel):
    """pip resolves `hostctl[deploy]` from the wheel's metadata, and `dev`
    must pull it, or the deploy tests skip instead of running."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata = archive.read(
            next(name for name in names if name.endswith(".dist-info/METADATA"))
        ).decode("utf-8")
    lines = [
        line for line in metadata.splitlines() if line.startswith("Requires-Dist:")
    ]
    deploy = [line for line in lines if re.search(r"extra == ['\"]deploy['\"]", line)]
    required = {
        re.match(r"Requires-Dist: ([A-Za-z0-9_.-]+)", line)[1] for line in deploy
    }

    assert "Provides-Extra: deploy" in metadata
    assert required == {"packaging", "pathspec", "tomli"}
    # tomllib is in the standard library from 3.11.
    assert "python_version" in next(line for line in deploy if "tomli" in line)
    # Hatchling inlines the self-referencing `hostctl[...]` of `dev`.
    dev = [line for line in lines if re.search(r"extra == ['\"]dev['\"]", line)]
    dev_required = {
        re.match(r"Requires-Dist: ([A-Za-z0-9_.-]+)", line)[1] for line in dev
    }
    assert {"packaging", "pathspec", "tomli"} <= dev_required
    assert "hostctl/deploy/__init__.py" in names


def test_the_wheel_never_ships_an_unshared_override(wheel, tmp_path):
    names = zipfile.ZipFile(wheel).namelist()

    assert [name for name in names if ".local." in name] == []


def test_the_shipped_header_names_every_public_export():
    """`src/hostctl/AGENTS.md` ships inside the wheel as `hostctl/AGENTS.md`
    and declares itself the stable surface -- "hosts and configs you
    construct, exceptions you catch, types you annotate with, and the
    provider/shell contracts you implement". It named 52 of 76: the entire
    provider-authoring contract was missing, `OperationNotStartedError`'s
    no-replay rule included, so a consuming agent reading the header instead
    of the source could not write a provider at all.
    """
    import importlib
    import pkgutil

    import hostctl

    header = (ROOT / "src" / "hostctl" / "AGENTS.md").read_text(encoding="utf-8")

    # Every public module's `__all__`, not only the root's: each is a promise.
    public = ["hostctl"] + [
        info.name
        for info in pkgutil.walk_packages(hostctl.__path__, "hostctl.")
        if not any(part.startswith("_") for part in info.name.split(".")[1:])
    ]
    missing = [
        f"{module}.{name}"
        for module in public
        for name in importlib.import_module(module).__all__
        if name not in header
    ]

    assert missing == []


@pytest.fixture(scope="module")
def sdist(tmp_path_factory, tree):
    """The other artifact. Both packaging leaks so far were found by hand.

    The wheel is what most consumers install, but the sdist is what a
    distro or a `--no-binary` build uses, and it is assembled by a
    different code path with a different exclusion list.
    """
    pytest.importorskip("hatchling", reason="the build backend is not installed")
    pytest.importorskip("build", reason="the build frontend is not installed")
    output = tmp_path_factory.mktemp("sdist")
    built = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "-n", "-o", str(output)],
        cwd=str(tree),
        capture_output=True,
        text=True,
    )
    if built.returncode != 0:
        pytest.skip(
            f"sdist build unavailable: {built.stdout[-400:]}{built.stderr[-400:]}"
        )
    archives = sorted(output.glob("*.tar.gz"))
    assert archives, built.stdout
    return archives[-1]


def _sdist_names(archive):
    with tarfile.open(archive) as value:
        # Strip the leading `hostctl-<version>/` component every sdist has.
        return [name.partition("/")[2] for name in value.getnames()]


def test_the_wheel_ships_the_typing_marker(wheel):
    """Without `py.typed`, an installed consumer's type checker ignores every
    annotation in the package -- silently, and only for installs."""
    assert "hostctl/py.typed" in zipfile.ZipFile(wheel).namelist()


def test_the_sdist_ships_what_a_source_build_needs(sdist):
    names = _sdist_names(sdist)

    assert "pyproject.toml" in names
    assert "src/hostctl/py.typed" in names
    assert "src/hostctl/AGENTS.md" in names
    assert "README.md" in names
    assert "LICENSE" in names


def test_the_sdist_never_ships_private_or_unshared_files(sdist):
    names = _sdist_names(sdist)

    assert [name for name in names if ".local." in name] == []
    # Built from parts so the repository's own leak scan does not flag it.
    private_dir = "." + "agents"
    assert [name for name in names if name.split("/")[0] == private_dir] == []
    assert [
        name for name in names if any(p.startswith("CLAUDE") for p in name.split("/"))
    ] == []


def test_the_sdist_leaves_out_contributor_files(sdist):
    """The root `AGENTS.md` and `CONTRIBUTING.md` orient a contributor to a
    checkout and `benchmarks/` is run on demand; none is part of a source
    build."""
    names = _sdist_names(sdist)

    assert "AGENTS.md" not in names
    assert "CONTRIBUTING.md" not in names
    assert [name for name in names if name.split("/")[0] == "benchmarks"] == []
    assert "src/hostctl/AGENTS.md" in names


def test_the_wheel_never_ships_an_assistant_file(wheel):
    names = zipfile.ZipFile(wheel).namelist()

    assert [
        name for name in names if any(p.startswith("CLAUDE") for p in name.split("/"))
    ] == []


_HEADING_MODULE = re.compile(r"^## .*\(`(hostctl[\w.]*)`\)\s*$")
_SIGNATURE_START = re.compile(r"^([A-Za-z_][\w.]*)\(")
_PARAMETER_KIND = {
    inspect.Parameter.POSITIONAL_ONLY: "positional-only",
    inspect.Parameter.POSITIONAL_OR_KEYWORD: "positional",
    inspect.Parameter.VAR_POSITIONAL: "*",
    inspect.Parameter.KEYWORD_ONLY: "keyword-only",
    inspect.Parameter.VAR_KEYWORD: "**",
}


def _header_signatures():
    """Yield `(line, module, name, parameter text)` for every signature in a
    ```python fence of the header, under the module its `##` heading names
    (`hostctl` where the heading names none)."""
    module = "hostctl"
    fenced = False
    lines = (ROOT / "src" / "hostctl" / "AGENTS.md").read_text("utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        heading = _HEADING_MODULE.match(line)
        if line.startswith("## "):
            module = heading.group(1) if heading else "hostctl"
        elif line.startswith("```"):
            fenced = line == "```python"
        elif fenced and _SIGNATURE_START.match(line):
            start, text = index + 1, line
            # A signature may wrap: join lines until its parentheses balance.
            while text.count("(") > text.count(")"):
                index += 1
                text += " " + lines[index].strip()
            name = _SIGNATURE_START.match(text).group(1)
            depth, end = 0, len(name)
            for end in range(len(name), len(text)):
                depth += {"(": 1, ")": -1}.get(text[end], 0)
                if depth == 0:
                    break
            yield start, module, name, text[len(name) + 1 : end]
        index += 1


def _resolve(module, name):
    """The object `name` denotes, and whether its first parameter is the
    implicit `self` that the header leaves out."""
    import importlib

    if name.startswith("hostctl."):
        parts = name.split(".")
        for split in range(len(parts) - 1, 0, -1):
            try:
                owner = importlib.import_module(".".join(parts[:split]))
            except ImportError:
                continue
            module, name = owner.__name__, ".".join(parts[split:])
            break
    owner = importlib.import_module(module)
    *path, last = name.split(".")
    for part in path:
        owner = getattr(owner, part)
    raw = inspect.getattr_static(owner, last)
    bound = inspect.isclass(owner) and inspect.isfunction(raw)
    return getattr(owner, last), bound


def _documented(parameters):
    tree = ast.parse(f"def _({parameters}): pass").body[0].args
    positional = [(a, "positional-only") for a in tree.posonlyargs]
    positional += [(a, "positional") for a in tree.args]
    defaulted = len(positional) - len(tree.defaults)
    shape = [
        (arg.arg, kind, number >= defaulted)
        for number, (arg, kind) in enumerate(positional)
    ]
    if tree.vararg:
        shape.append((tree.vararg.arg, "*", False))
    shape += [
        (arg.arg, "keyword-only", default is not None)
        for arg, default in zip(tree.kwonlyargs, tree.kw_defaults)
    ]
    if tree.kwarg:
        shape.append((tree.kwarg.arg, "**", False))
    return shape


def _actual(target, bound):
    parameters = list(inspect.signature(target).parameters.values())
    if bound:
        parameters = parameters[1:]
    return [
        (
            parameter.name,
            _PARAMETER_KIND[parameter.kind],
            parameter.default is not inspect.Parameter.empty,
        )
        for parameter in parameters
    ]


def test_the_header_documents_signatures():
    """The scan finds the entries it exists for, so an unparsed header cannot
    pass the comparison below by yielding nothing."""
    names = {name for _, _, name, _ in _header_signatures()}

    assert {"SSHConfig", "Shell", "uri_hostname", "write_output"} <= names
    assert len(names) > 100


@pytest.mark.parametrize(
    "line, module, name, parameters",
    [pytest.param(*entry, id=entry[2]) for entry in _header_signatures()],
)
def test_the_header_signatures_match_the_code(line, module, name, parameters):
    """The header is what a consuming agent reads instead of the source, so
    every signature in it states the real parameter names in order, where
    `/` and `*` fall, and which parameters have a default. Default values
    and annotations are not compared."""
    target, bound = _resolve(module, name)

    assert _documented(parameters) == _actual(target, bound), (
        f"src/hostctl/AGENTS.md:{line}: {name}({parameters}) is not "
        f"{name}{inspect.signature(target)}"
    )
