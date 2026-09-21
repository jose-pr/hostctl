"""Drive `WinRMPathBackend`'s generated scripts through a real PowerShell.

Every other WinRM path test *interprets* the generated script instead of
running it, which is why four PowerShell-level defects passed a green suite:
the `$null` backup name that made every overwrite throw, the
`MethodInvocationException` wrapper that degraded every .NET-API error to bare
OSError, and the reparse-point guard that refused to list a junction.

There is no remote host here. The scripts are the ones the transport would
send; only the hop is replaced, by running them locally against a temp
directory. That is the half a fake cannot prove.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from hostctl.host._winrm import WinRMPathBackend

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="the generated scripts are Windows PowerShell"
)


def _powershell_runner(script, *, check=False, encoding="utf-8", **_options):
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding=encoding,
        check=False,
    )


@pytest.fixture
def backend():
    return WinRMPathBackend(_powershell_runner)


def test_write_bytes_creates_then_overwrites(backend, tmp_path):
    """The overwrite branch threw on every PowerShell version."""
    target = str(tmp_path / "config.ini")

    backend.write_bytes(target, b"first")
    assert backend.read_bytes(target) == b"first"

    backend.write_bytes(target, b"second")
    assert backend.read_bytes(target) == b"second"


def test_exclusive_write_refuses_an_existing_file(backend, tmp_path):
    target = str(tmp_path / "once.txt")
    backend.write_bytes(target, b"first")

    with pytest.raises(FileExistsError):
        backend.write_bytes(target, b"second", exclusive=True)

    assert backend.read_bytes(target) == b"first"


def test_reading_a_missing_file_raises_file_not_found(backend, tmp_path):
    """It degraded to bare OSError, which defeats staged_open's guard --
    so `open("ab")` on a missing file raised instead of creating it."""
    with pytest.raises(FileNotFoundError):
        backend.read_bytes(str(tmp_path / "no-such-file.txt"))


def test_appending_to_a_missing_file_creates_it(backend, tmp_path):
    """The consequence of the classification bug, end to end."""
    from hostctl.host._staged_io import staged_open

    target = str(tmp_path / "audit.log")

    with staged_open(backend, target, "a", label="WinRM") as stream:
        stream.write(b"entry\n")

    assert backend.read_bytes(target) == b"entry\n"


def test_listing_a_directory_junction_works(backend, tmp_path):
    """Any reparse point reported as S_IFLNK, so the guard refused to list
    a junction even though `is_dir()` on the same path returned True."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "inside.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "link"

    made = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(real)],
        capture_output=True,
        text=True,
    )
    if made.returncode != 0:
        pytest.skip(f"cannot create a junction here: {made.stdout}{made.stderr}")

    names = [name for name, _stat in backend.scandir(str(link))]

    assert names == ["inside.txt"]


def test_stat_and_scandir_agree_about_a_junction(backend, tmp_path):
    import stat as stat_module

    real = tmp_path / "real2"
    real.mkdir()
    link = tmp_path / "link2"
    made = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(real)],
        capture_output=True,
        text=True,
    )
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")

    followed = backend.stat(str(link), follow_symlinks=True)

    assert stat_module.S_ISDIR(followed.st_mode)
    assert backend.scandir(str(link)) == []
