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

from conformance.providers import LocalWinRSProtocol
from hostctl import WinRMConfig
from hostctl.executor.winrm import _PywinrmStdinSession
from hostctl.host._winrm import _SCRIPT_BUDGET, _WinRMTransport

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="the generated scripts are Windows PowerShell"
)


@pytest.fixture
def transport():
    """The production pywinrm leg; only the WinRS hop is a local process."""
    built = _WinRMTransport(
        WinRMConfig("fake", "fake", "fake", provider="pywinrm", transport="ntlm")
    )
    built._session = _PywinrmStdinSession(LocalWinRSProtocol())
    return built


@pytest.fixture
def backend(transport):
    return transport._path_backend


@pytest.mark.parametrize("size", [0, 2, 5000, 1 << 20])
def test_a_write_round_trips_at_the_production_budget(transport, tmp_path, size):
    """Every pywinrm write used to raise OSError: the budget was smaller than
    the minimum the writer needed."""
    backend = transport._path_backend
    assert backend.max_script_bytes == _SCRIPT_BUDGET
    target = str(tmp_path / "blob.bin")
    data = os.urandom(size)

    backend.write_bytes(target, data)

    assert backend.read_bytes(target) == data
    assert [entry.name for entry in tmp_path.iterdir()] == ["blob.bin"]


def test_a_long_command_is_not_bounded_by_a_command_line(transport):
    """cmd.exe rejected a `powershell -encodedcommand` line past ~8 KB."""
    result = transport.run(
        "$x='" + "z" * 6000 + "'; Write-Output $x.Length",
        check=False,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "6000"


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

    with staged_open(backend, target, "a") as stream:
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


def test_unlinking_a_populated_directory_junction_removes_only_the_link(
    backend, tmp_path
):
    """`Remove-Item -Force` on a *directory* reparse point whose target has
    entries treats the link as a directory with children and prompts for
    confirmation. Under `-NonInteractive` -- how every WinRM hop runs
    powershell.exe -- the prompt cannot be answered, so PowerShell raised and
    the junction stayed. The same call succeeded when the target happened to
    be empty, so the failure looked intermittent."""
    real = tmp_path / "release"
    real.mkdir()
    (real / "app.exe").write_text("payload", encoding="utf-8")
    link = tmp_path / "current"

    made = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(real)],
        capture_output=True,
        text=True,
    )
    if made.returncode != 0:
        pytest.skip(f"cannot create a junction here: {made.stdout}{made.stderr}")

    backend.unlink(str(link))

    assert not link.exists()
    assert (real / "app.exe").read_text(encoding="utf-8") == "payload"
