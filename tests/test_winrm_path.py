"""Windows path semantics and pathlib operations over a fake WinRM backend."""

import stat
import subprocess
import json

import pytest
from pathlib_next import Path
from pathlib_next.utils.stat import FileStat

from hostctl import WinRMPath, WinRMPathBackend


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


def test_winrm_path_closes_buffer_when_writeback_fails():
    class _FailingBackend(_MemoryBackend):
        def write_bytes(self, path, value, *, exclusive=False):
            raise OSError("upload failed")

    path = WinRMPath(r"C:\data.bin", backend=_FailingBackend())
    stream = path.open("wb")
    stream.write(b"data")

    with pytest.raises(OSError, match="upload failed"):
        stream.close()

    assert stream.closed


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
def test_winrm_backend_write_chunk_boundaries_stay_within_budget(size):
    scripts = []

    def run(script, **kwargs):
        scripts.append(script)
        return subprocess.CompletedProcess(script, 0, "", "")

    backend = WinRMPathBackend(run)
    backend.write_bytes(r"C:\boundary.bin", b"x" * size)
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
