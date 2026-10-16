"""Drive `WinRMPathBackend`'s generated scripts through a real PowerShell.

Every other WinRM path test *interprets* the generated script instead of
running it, so a PowerShell-level defect passes them: a `$null` backup name
that makes every overwrite throw, a `MethodInvocationException` wrapper that
degrades every .NET-API error to bare OSError, a reparse-point guard that
refuses to list a junction.

There is no remote host here. The scripts are the ones the transport would
send; only the hop is replaced, by running them locally against a temp
directory. That is the half a fake cannot prove.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from conformance.providers import LocalWinRSProtocol
from hostctl.transports.winrm._transport import WinRMConfig
from hostctl.transports.winrm._pywinrm import _PywinrmStdinSession
from hostctl.transports.winrm._transport import _SCRIPT_BUDGET, _WinRMTransport

pytestmark = pytest.mark.skipif(
    shutil.which("powershell.exe") is None or shutil.which("cmd.exe") is None,
    reason="the generated scripts are Windows PowerShell; cmd.exe makes junctions",
)


@pytest.fixture
def transport():
    """The production pywinrm leg; only the WinRS hop is a local process."""
    built = _WinRMTransport(
        WinRMConfig("fake", "fake", "fake", provider="pywinrm", transport="ntlm")
    )
    built._leg._session = _PywinrmStdinSession(LocalWinRSProtocol())
    return built


@pytest.fixture
def backend(transport):
    return transport._path_backend


@pytest.mark.parametrize("size", [0, 2, 5000, 1 << 20])
def test_a_write_round_trips_at_the_production_budget(transport, tmp_path, size):
    """The production script budget is above the minimum the writer needs,
    or every pywinrm write raises OSError."""
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
    from hostctl.paths._staged_io import staged_open

    target = str(tmp_path / "audit.log")

    with staged_open(backend, target, "a") as stream:
        stream.write(b"entry\n")

    assert backend.read_bytes(target) == b"entry\n"


def test_listing_a_directory_junction_works(backend, tmp_path):
    """Any reparse point reports as S_IFLNK; a junction must still list,
    as `is_dir()` on the same path says it is a directory."""
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


def test_removing_a_non_empty_directory_is_enotempty(backend, tmp_path):
    import errno

    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "child").write_text("x")

    with pytest.raises(OSError) as raised:
        backend.rmdir(str(tmp_path / "full"))

    assert raised.value.errno == errno.ENOTEMPTY
    assert not isinstance(raised.value, (FileExistsError, PermissionError))


def test_renaming_onto_an_existing_entry_is_file_exists(backend, tmp_path):
    (tmp_path / "a").write_text("1")
    (tmp_path / "b").write_text("2")
    (tmp_path / "d1").mkdir()
    (tmp_path / "d2").mkdir()

    with pytest.raises(FileExistsError):
        backend.rename(str(tmp_path / "a"), str(tmp_path / "b"))
    with pytest.raises(FileExistsError):
        backend.rename(str(tmp_path / "d1"), str(tmp_path / "d2"))


def test_scandir_of_a_file_is_enotdir(backend, tmp_path):
    import errno

    (tmp_path / "f").write_text("x")

    with pytest.raises(NotADirectoryError) as raised:
        backend.scandir(str(tmp_path / "f"))

    assert raised.value.errno == errno.ENOTDIR


def test_exists_answers_from_metadata(backend, tmp_path):
    (tmp_path / "f").write_text("x")

    assert backend.exists(str(tmp_path / "f")) is True
    assert backend.exists(str(tmp_path / "missing")) is False


def test_exclusive_open_of_an_existing_file_downloads_nothing(transport, tmp_path):
    """`open("xb")` read the whole existing file just to learn it was there."""
    from hostctl.transports.winrm._transport import WinRMPath

    target = tmp_path / "big.bin"
    target.write_bytes(os.urandom(1 << 20))
    backend = transport._path_backend
    returned = []
    run = backend._run

    def spy(script, **options):
        result = run(script, **options)
        returned.append(len(result.stdout or ""))
        return result

    backend._run = spy
    with pytest.raises(FileExistsError):
        WinRMPath(str(target), backend=backend).open("xb")

    assert returned and sum(returned) < 4096


def test_a_read_from_an_offset_is_one_script(backend, tmp_path):
    data = os.urandom(300_000)
    (tmp_path / "f").write_bytes(data)

    assert backend.read_bytes(str(tmp_path / "f"), 123_456) == data[123_456:]
    with pytest.raises(ValueError):
        backend.read_bytes(str(tmp_path / "f"), -1)
