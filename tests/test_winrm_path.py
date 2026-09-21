"""Windows path semantics and pathlib operations over a fake WinRM backend."""

import os
import stat
import base64
import subprocess
import json

import pytest
from pathlib_next import Path
from pathlib_next.utils.stat import FileStat

from hostctl import WinRMPath
from hostctl.host._winrm import WinRMPathBackend


class _MemoryBackend:
    def __init__(self):
        self.files = {}
        self.directories = {"C:\\"}

    def stat(self, path, *, follow_symlinks=True):
        if path in self.directories:
            return FileStat(st_mode=stat.S_IFDIR | 0o777, is_dir=True)
        if path in self.files:
            return FileStat(st_mode=stat.S_IFREG | 0o666, st_size=len(self.files[path]))
        raise FileNotFoundError(path)

    def scandir(self, path):
        prefix = path.rstrip("\\") + "\\"
        result = []
        for value in sorted(self.directories | set(self.files)):
            remainder = value[len(prefix) :] if value.startswith(prefix) else ""
            if remainder and "\\" not in remainder:
                result.append((remainder, self.stat(value)))
        return result

    def read_bytes(self, path):
        try:
            return self.files[path]
        except KeyError:
            raise FileNotFoundError(path)

    def write_bytes(self, path, value, *, exclusive=False):
        if exclusive and path in self.files:
            raise FileExistsError(path)
        self.files[path] = value

    def mkdir(self, path):
        if path in self.directories or path in self.files:
            raise FileExistsError(path)
        self.directories.add(path)

    def unlink(self, path, *, missing_ok=False):
        if path in self.directories:
            raise IsADirectoryError(path)
        if path not in self.files:
            if missing_ok:
                return
            raise FileNotFoundError(path)
        del self.files[path]

    def rmdir(self, path):
        self.directories.remove(path)

    def rename(self, path, target):
        if path in self.files:
            self.files[target] = self.files.pop(path)
        else:
            self.directories.remove(path)
            self.directories.add(target)

    def chmod(self, path, mode):
        self.stat(path)


def test_winrm_path_is_pathlib_next_windows_path_and_propagates_backend():
    backend = _MemoryBackend()
    path = WinRMPath(r"C:\Temp\Folder", backend=backend)

    assert isinstance(path, Path)
    assert path.drive == "C:"
    assert path.parent == WinRMPath(r"C:\Temp", backend=backend)
    assert (path / "child").backend is backend
    assert path.match(r"c:\temp\folder")


def test_winrm_path_read_write_append_exclusive_and_traversal():
    backend = _MemoryBackend()
    root = WinRMPath(r"C:\\", backend=backend)
    directory = root / "Temp"
    directory.mkdir()
    path = directory / "data.bin"

    assert path.write_bytes(b"\x00abc") == 4
    assert path.read_bytes() == b"\x00abc"
    with path.open("ab") as stream:
        stream.write(b"!")
    assert path.read_bytes() == b"\x00abc!"
    assert [child.name for child in directory.iterdir()] == ["data.bin"]

    renamed = path.rename(directory / "renamed.bin")
    assert renamed.read_bytes() == b"\x00abc!"
    renamed.unlink()
    assert not renamed.exists()


@pytest.mark.parametrize("mode", ("", "rw", "ra", "wx", "rr", "++", "r++"))
def test_winrm_path_rejects_invalid_open_modes(mode):
    path = WinRMPath(r"C:\data.bin", backend=_MemoryBackend())

    with pytest.raises(ValueError):
        path.open(mode)


def test_winrm_path_unlink_rejects_directory_even_with_missing_ok():
    backend = _MemoryBackend()
    path = WinRMPath(r"C:\\", backend=backend)

    with pytest.raises(IsADirectoryError):
        path.unlink(missing_ok=True)


def test_winrm_path_keeps_the_buffer_when_writeback_fails():
    """The staged bytes are the only copy. Closing the buffer in a `finally`
    made them unrecoverable after a transient upload error, while a retried
    `close()` returned silently having written nothing."""

    class _FailingBackend(_MemoryBackend):
        def write_bytes(self, path, value, *, exclusive=False):
            raise OSError("upload failed")

    path = WinRMPath(r"C:\data.bin", backend=_FailingBackend())
    stream = path.open("wb")
    stream.write(b"data")

    with pytest.raises(OSError, match="upload failed"):
        stream.close()

    assert not stream.closed
    assert stream.getvalue() == b"data"
    with pytest.raises(OSError, match="upload failed"):
        stream.close()
    stream.discard()
    stream.close()


def test_winrm_backend_prelude_and_command_budget_for_large_write():
    scripts = []

    def run(script, **kwargs):
        scripts.append(script)
        return subprocess.CompletedProcess(script, 0, "", "")

    backend = WinRMPathBackend(run)
    backend.write_bytes(r"C:\large.bin", b"x" * (1024 * 1024))
    assert scripts
    assert all(
        len(script.encode("utf-8")) <= backend.max_script_bytes for script in scripts
    )
    assert all("OutputEncoding" in script for script in scripts)
    assert all(
        "try{[Console]::OutputEncoding=" in script
        and "catch{};$OutputEncoding=" in script
        for script in scripts
    )
    # Multiple chunks are grouped into a single PowerShell invocation.
    assert len(scripts) < 1024


@pytest.mark.parametrize(
    ("marker", "error_type"),
    [
        ("missing", FileNotFoundError),
        ("permission", PermissionError),
        ("exists", FileExistsError),
        ("isdir", IsADirectoryError),
        ("notdir", NotADirectoryError),
    ],
)
def test_winrm_backend_marker_error_mapping(marker, error_type):
    encoded = __import__("base64").b64encode(b"detail").decode("ascii")

    def run(script, **kwargs):
        return subprocess.CompletedProcess(
            script, 0, f"HOSTCTL_ERROR:{marker}:{encoded}", ""
        )

    backend = WinRMPathBackend(run)
    with pytest.raises(error_type, match="detail"):
        backend.stat(r"C:\hostile'$(rm x).txt")


def test_winrm_backend_hostile_path_is_encoded_and_json_stat_is_parsed():
    scripts = []

    def run(script, **kwargs):
        scripts.append(script)
        value = {
            "name": "smart’quote.txt",
            "directory": False,
            "size": 3,
            "mtime": 12,
            "readonly": False,
            "link": False,
            "target": "",
        }
        return subprocess.CompletedProcess(script, 0, json.dumps(value), "")

    backend = WinRMPathBackend(run)
    result = backend.stat(r"C:\foo'$(rm x).txt")
    assert result.st_size == 3
    assert "foo'$(rm x)" not in scripts[0]
    assert "OutputEncoding" in scripts[0]


@pytest.mark.parametrize("size", [0, 1535, 1536, 1537, 3072])
@pytest.mark.parametrize(
    "path",
    [
        r"C:\boundary.bin",
        # A long path: a script's fixed cost is its PRELUDE, which
        # carries the base64 of the path itself, so a flat reserve was
        # smaller than the real overhead here and the emitted script
        # broke the very budget this test exists to check.
        "C:\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\directory-with-a-long-name\\payload.bin",
    ],
    ids=["short", "long"],
)
def test_winrm_backend_write_chunk_boundaries_stay_within_budget(size, path):
    scripts = []

    def run(script, **kwargs):
        scripts.append(script)
        return subprocess.CompletedProcess(script, 0, "", "")

    backend = WinRMPathBackend(run)
    backend.write_bytes(path, b"x" * size)
    assert all(
        len(script.encode("utf-8")) <= backend.max_script_bytes for script in scripts
    )


def test_winrm_open_read_uses_lazy_range_requests():
    scripts = []

    def run(script, **kwargs):
        scripts.append(script)
        payload = __import__("base64").b64encode(b"ab").decode("ascii")
        return subprocess.CompletedProcess(script, 0, payload, "")

    backend = WinRMPathBackend(run)
    with backend.open_read(r"C:\large.bin") as stream:
        assert stream.read(2) == b"ab"
    assert len(scripts) == 1
    assert "Seek(0" in scripts[0]


@pytest.mark.skipif(os.name != "nt", reason="runs the generated script in PowerShell")
def test_the_overwrite_commit_actually_replaces_a_file(tmp_path):
    """`[IO.File]::Replace($p,$t,$null)` throws on every PowerShell version.

    PowerShell's .NET binder turns `$null` into "" for a `string` parameter,
    so `Replace` got an empty backup name and raised -- meaning `write_bytes`
    over an existing file, and `open('wb'/'ab'/'r+b')`, could never succeed.
    Only a first write to a missing path worked. This runs the committed form
    through a real PowerShell rather than asserting on its text.
    """
    import subprocess

    target = tmp_path / "target.txt"
    staged = tmp_path / "staged.tmp"
    target.write_text("old", encoding="utf-8")
    staged.write_text("new", encoding="utf-8")

    script = (
        f"$p='{staged}'; $t='{target}'; "
        "if([IO.File]::Exists($t)){"
        "[IO.File]::Replace($p,$t,[NullString]::Value)}"
        "else{[IO.File]::Move($p,$t)}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == "new"
    assert not staged.exists()


@pytest.mark.skipif(os.name != "nt", reason="runs the generated script in PowerShell")
def test_the_old_null_backup_form_is_the_one_that_failed(tmp_path):
    """The positive control for the fix above."""
    import subprocess

    target = tmp_path / "target.txt"
    staged = tmp_path / "staged.tmp"
    target.write_text("old", encoding="utf-8")
    staged.write_text("new", encoding="utf-8")

    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"[IO.File]::Replace('{staged}','{target}',$null)",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0 or "Exception" in result.stderr
    assert target.read_text(encoding="utf-8") == "old"


def test_symlink_to_force_reaches_the_backend():
    """The override took the PUBLIC name, so `pathlib_next`'s wrapper --
    which is where `force=` (remove an existing entry first) and target
    normalisation live -- never ran: `symlink_to(target, force=True)`
    silently ignored `force`."""
    removed = []

    class _Backend(_MemoryBackend):
        def symlink(self, path, target):
            self.files[path] = b""

        def unlink(self, path, *, missing_ok=False):
            removed.append(path)
            self.files.pop(path, None)

        def stat(self, path, *, follow_symlinks=True):
            import stat as stat_module

            from pathlib_next.utils.stat import FileStat

            if path not in self.files:
                raise FileNotFoundError(path)
            return FileStat(st_mode=stat_module.S_IFREG | 0o644, st_size=0)

    backend = _Backend()
    backend.files[r"C:\link"] = b"existing"
    path = WinRMPath(r"C:\link", backend=backend)

    path.symlink_to(r"C:\target", force=True)

    assert removed == [r"C:\link"]


def test_a_mapped_error_carries_errno_and_the_path():
    """The mapped subclasses were raised with only a message, so
    `exc.errno == errno.ENOENT` was False and `exc.filename` was None -- a
    caller had the class and whatever text the remote happened to send."""
    import errno

    def run(script, **kwargs):
        marker = base64.b64encode(b"no such file").decode("ascii")
        return subprocess.CompletedProcess(
            script, 0, f"HOSTCTL_ERROR:missing:{marker}", ""
        )

    backend = WinRMPathBackend(run)

    with pytest.raises(FileNotFoundError) as raised:
        backend.read_bytes(r"C:\absent.txt")

    assert raised.value.errno == errno.ENOENT
    assert raised.value.filename == r"C:\absent.txt"
    assert "no such file" in str(raised.value)


def test_a_relative_link_target_resolves_against_the_links_directory():
    r"""Windows resolves a relative link target against the LINK's directory.
    Re-stating it as an absolute path looked it up from the drive root, so
    `C:\app\current -> releases\v3` stat'd `releases\v3`."""
    import stat as stat_module

    seen = []

    class _Backend(WinRMPathBackend):
        def __init__(self):
            super().__init__(lambda script, **kwargs: None)

        def _execute(self, path, body, **kwargs):
            seen.append(path)
            if path == r"C:\app\current":
                return json.dumps({"mode": "l", "link": True, "target": r"releases\v3"})
            return json.dumps({"mode": "d", "size": 0})

        def _stat_value(self, value):
            if value.get("link"):
                return FileStat(st_mode=stat_module.S_IFLNK | 0o777)
            return FileStat(st_mode=stat_module.S_IFDIR | 0o755, is_dir=True)

    backend = _Backend()
    backend.stat(r"C:\app\current", follow_symlinks=True)

    assert seen == [r"C:\app\current", r"C:\app\releases\v3"]


def test_a_self_referential_link_stops_rather_than_recursing():
    import errno
    import stat as stat_module

    class _Backend(WinRMPathBackend):
        def __init__(self):
            super().__init__(lambda script, **kwargs: None)

        def _execute(self, path, body, **kwargs):
            return json.dumps({"mode": "l", "link": True, "target": path})

        def _stat_value(self, value):
            return FileStat(st_mode=stat_module.S_IFLNK | 0o777)

    with pytest.raises(OSError) as raised:
        _Backend().stat(r"C:\loop", follow_symlinks=True)

    assert raised.value.errno == errno.ELOOP
