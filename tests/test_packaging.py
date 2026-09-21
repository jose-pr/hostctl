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

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    pytest.importorskip("hatchling", reason="the build backend is not installed")
    pytest.importorskip("build", reason="the build frontend is not installed")
    output = tmp_path_factory.mktemp("dist")
    # Plant the file the exclusion exists for, so the assertion below is
    # never vacuous. It is ignored by git and removed again below.
    planted = ROOT / "src" / "hostctl" / "AGENTS.local.md"
    planted_existed = planted.exists()
    if not planted_existed:
        planted.write_text("machine-specific notes" + chr(10), encoding="utf-8")
    # `-n`: no isolation, so this needs no network and uses the backend that
    # is already installed.
    built = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "-n", "-o", str(output)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if built.returncode != 0:
        if not planted_existed:
            planted.unlink(missing_ok=True)
        pytest.skip(
            f"wheel build unavailable: {built.stdout[-400:]}{built.stderr[-400:]}"
        )
    wheels = sorted(output.glob("*.whl"))
    assert wheels, built.stdout
    try:
        yield wheels[-1]
    finally:
        if not planted_existed:
            planted.unlink(missing_ok=True)


def test_the_wheel_ships_the_api_header(wheel):
    names = zipfile.ZipFile(wheel).namelist()

    assert "hostctl/AGENTS.md" in names
    assert "hostctl/README.md" in names


def test_the_wheel_never_ships_an_unshared_override(wheel, tmp_path):
    names = zipfile.ZipFile(wheel).namelist()

    assert [name for name in names if ".local." in name] == []


def test_the_shipped_header_names_every_public_export():
    """`src/hostctl/AGENTS.md` ships inside the wheel as `hostctl/AGENTS.md`
    and declares itself the stable surface -- "hosts and configs you
    construct, exceptions you catch, types you annotate with, and the
    provider/shell contracts you implement". It named 52 of 76: the entire
    provider-authoring contract was missing, `OperationNotStarted`'s
    no-replay rule included, so a consuming agent reading the header instead
    of the source could not write a provider at all.
    """
    import hostctl

    header = (ROOT / "src" / "hostctl" / "AGENTS.md").read_text(encoding="utf-8")

    missing = [name for name in hostctl.__all__ if name not in header]

    assert missing == []


@pytest.fixture(scope="module")
def sdist(tmp_path_factory):
    """The other artifact. Both packaging leaks so far were found by hand.

    The wheel is what most consumers install, but the sdist is what a
    distro or a `--no-binary` build uses, and it is assembled by a
    different code path with a different exclusion list.
    """
    pytest.importorskip("hatchling", reason="the build backend is not installed")
    pytest.importorskip("build", reason="the build frontend is not installed")
    output = tmp_path_factory.mktemp("sdist")
    planted = ROOT / "src" / "hostctl" / "AGENTS.local.md"
    planted_existed = planted.exists()
    if not planted_existed:
        planted.write_text("machine-specific notes" + chr(10), encoding="utf-8")
    built = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "-n", "-o", str(output)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if built.returncode != 0:
        if not planted_existed:
            planted.unlink(missing_ok=True)
        pytest.skip(
            f"sdist build unavailable: {built.stdout[-400:]}{built.stderr[-400:]}"
        )
    archives = sorted(output.glob("*.tar.gz"))
    assert archives, built.stdout
    try:
        yield archives[-1]
    finally:
        if not planted_existed:
            planted.unlink(missing_ok=True)


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
    assert [name for name in names if name.split("/")[0] == ".agents"] == []
    assert [name for name in names if name.startswith("CLAUDE")] == []


@pytest.mark.parametrize(
    "name",
    ("SshConfig", "WinRMConfig", "ContainerConfig", "QemuConfig", "SerialConfig"),
)
def test_the_header_names_every_constructor_parameter(name):
    """The header is what a consuming agent reads instead of the source.

    Four signatures there had drifted: `client_factory`, `max_reply_size`,
    `transport_factory`, `serial_console` and `path_helper` were absent, and
    `SerialConfig` had no signature at all -- so the injection seams a
    caller needs were discoverable nowhere but the source.
    """
    import dataclasses
    import re

    import hostctl

    header = (ROOT / "src" / "hostctl" / "AGENTS.md").read_text(encoding="utf-8")
    match = re.search(rf"`{name}\((.*?)\)`", header, re.DOTALL)
    assert match, f"{name} has no documented signature"
    documented = match.group(1)

    missing = [
        field.name
        for field in dataclasses.fields(getattr(hostctl, name))
        if not field.name.startswith("_")
        and not re.search(rf"\b{re.escape(field.name)}\b", documented)
    ]

    assert missing == [], f"{name} signature omits {missing}"
