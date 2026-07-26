import base64
import io
import stat

import pytest
from pathlib_next import Path
from pathlib_next.utils.stat import FileStat

from hostctl import (
    PosixQemuPath,
    QgaPathBackend,
    WindowsQemuPath,
)
from hostctl.executor._qga import QgaCommandError

FILE_COMMANDS = {
    "guest-file-open",
    "guest-file-read",
    "guest-file-write",
    "guest-file-seek",
    "guest-file-flush",
    "guest-file-close",
}


class _QgaError(Exception):
    def __init__(self, name, message):
        super().__init__(message)
        self.name = name


class _Transport:
    def __init__(self):
        self.files = {}
        self.handles = {}
        self.next_handle = 1
        self.calls = []
        self.fail_read = False

    def execute(self, request, timeout=None):
        self.calls.append((request, timeout))
        command = request["execute"]
        arguments = request.get("arguments", {})
        if command == "guest-file-open":
            path = arguments["path"]
            mode = arguments["mode"]
            if "r" in mode and path not in self.files:
                raise _QgaError("GenericErrorNotFound", path)
            if "w" in mode:
                self.files[path] = bytearray()
            handle = self.next_handle
            self.next_handle += 1
            self.handles[handle] = [path, 0]
            return handle
        handle = arguments["handle"]
        path, offset = self.handles[handle]
        if command == "guest-file-read":
            if self.fail_read:
                raise _QgaError("PermissionDenied", path)
            data = bytes(self.files[path][offset : offset + arguments["count"]])
            self.handles[handle][1] += len(data)
            return {
                "count": len(data),
                "buf-b64": base64.b64encode(data).decode(),
                "eof": self.handles[handle][1] >= len(self.files[path]),
            }
        if command == "guest-file-write":
            data = base64.b64decode(arguments["buf-b64"])
            # Exercise partial-write retry.
            count = max(1, len(data) // 2)
            self.files[path].extend(data[:count])
            return {"count": count}
        if command == "guest-file-seek":
            whence = arguments["whence"]
            whence = (
                {"set": 0, "cur": 1, "end": 2}[whence["name"]]
                if isinstance(whence, dict)
                else whence
            )
            base = (0, offset, len(self.files[path]))[whence]
            self.handles[handle][1] = base + arguments["offset"]
            return {"position": self.handles[handle][1], "eof": False}
        if command == "guest-file-flush":
            return {}
        if command == "guest-file-close":
            del self.handles[handle]
            return {}
        raise AssertionError(command)


class _Helper:
    def __init__(self, transport):
        self.transport = transport
        self.directories = {"/", r"C:\\"}
        self.renames = []

    def stat(self, path, *, follow_symlinks=True):
        if path in self.directories:
            return FileStat(st_mode=stat.S_IFDIR | 0o755, st_size=0)
        if path not in self.transport.files:
            raise FileNotFoundError(path)
        return FileStat(
            st_mode=stat.S_IFREG | 0o644,
            st_size=len(self.transport.files[path]),
        )

    def scandir(self, path):
        return [("snow \N{SNOWMAN}.txt", self.stat("/snow \N{SNOWMAN}.txt"))]

    def mkdir(self, path, mode):
        self.directories.add(path)

    def unlink(self, path, *, missing_ok=False):
        if path not in self.transport.files and not missing_ok:
            raise FileNotFoundError(path)
        self.transport.files.pop(path, None)

    def rmdir(self, path):
        self.directories.remove(path)

    def rename(self, path, target, *, replace=False):
        if target in self.transport.files and not replace:
            raise FileExistsError(target)
        self.transport.files[target] = self.transport.files.pop(path)
        self.renames.append((path, target, replace))

    def chmod(self, path, mode, *, follow_symlinks=True):
        return None


def test_bounded_binary_transfer_retries_partial_writes_and_closes_handles():
    transport = _Transport()
    backend = QgaPathBackend(transport, supported_commands=FILE_COMMANDS, chunk_size=3)
    path = PosixQemuPath("/data/snow \N{SNOWMAN}.bin", backend=backend)

    path.write_bytes(b"\x00abcdef\xff")

    assert path.read_bytes() == b"\x00abcdef\xff"
    assert transport.handles == {}
    write_calls = [
        call for call, _ in transport.calls if call["execute"] == "guest-file-write"
    ]
    assert len(write_calls) > 3


def test_qga_open_read_fetches_incrementally():
    transport = _Transport()
    transport.files["/large"] = bytearray(b"x" * 64)
    backend = QgaPathBackend(
        transport,
        supported_commands=FILE_COMMANDS,
        chunk_size=4,
    )
    path = PosixQemuPath("/large", backend=backend)
    with path.open("rb") as stream:
        assert stream.read(1) == b"x"
        reads = [
            call for call, _ in transport.calls if call["execute"] == "guest-file-read"
        ]
        assert len(reads) == 1
        assert reads[0]["arguments"]["count"] == 4


def test_read_error_is_normalized_and_handle_is_closed():
    transport = _Transport()
    transport.files["/secret"] = bytearray(b"value")
    transport.fail_read = True
    backend = QgaPathBackend(transport, supported_commands=FILE_COMMANDS)

    with pytest.raises(PermissionError) as caught:
        backend.read_bytes("/secret")

    assert isinstance(caught.value.__cause__, _QgaError)
    assert transport.handles == {}


@pytest.mark.parametrize(
    ("error_class", "description", "expected"),
    [
        ("GenericError", "No such file or directory", FileNotFoundError),
        ("GenericError", "The system cannot find the file", FileNotFoundError),
        ("PermissionDenied", "operation failed", PermissionError),
        ("GenericError", "Access is denied", PermissionError),
        ("GenericError", "File exists", FileExistsError),
        ("GenericError", "Is a directory", IsADirectoryError),
    ],
)
def test_structured_qga_error_is_normalized(error_class, description, expected):
    class _DeniedTransport:
        def execute(self, request, timeout=None):
            raise QgaCommandError(error_class, description)

    backend = QgaPathBackend(_DeniedTransport(), supported_commands=FILE_COMMANDS)

    with pytest.raises(expected, match=description) as caught:
        backend.read_bytes("/secret")

    assert isinstance(caught.value.__cause__, QgaCommandError)


def test_commands_and_helpers_are_capability_gated():
    transport = _Transport()
    backend = QgaPathBackend(transport, supported_commands={"guest-file-open"})
    path = PosixQemuPath("/value", backend=backend)

    with pytest.raises(NotImplementedError, match="guest-file-read"):
        path.read_bytes()
    with pytest.raises(NotImplementedError, match="positively probed"):
        path.stat()
    with pytest.raises(NotImplementedError, match="exclusive"):
        with path.open("xb") as stream:
            stream.write(b"value")


def test_transactional_write_cleanup_and_exclusive_mode():
    transport = _Transport()
    helper = _Helper(transport)
    backend = QgaPathBackend(transport, supported_commands=FILE_COMMANDS, helper=helper)
    path = PosixQemuPath("/value", backend=backend)

    path.write_text("first")
    with pytest.raises(FileExistsError):
        with path.open("x") as stream:
            stream.write("second")

    assert path.read_text() == "first"
    assert not any(".hostctl-" in name for name in transport.files)
    assert helper.renames[0][2] is True


def test_path_flavours_and_backend_survive_derivation():
    transport = _Transport()
    helper = _Helper(transport)
    backend = QgaPathBackend(transport, supported_commands=FILE_COMMANDS, helper=helper)
    posix = PosixQemuPath("/srv", backend=backend)
    windows = WindowsQemuPath(r"C:\Temp", backend=backend)

    assert isinstance(posix, Path)
    assert isinstance(windows, Path)
    assert str(posix / "child") == "/srv/child"
    assert str(windows / "child") == r"C:\Temp\child"
    assert (posix / "child").backend is backend
    assert (windows / "child").parent.backend is backend


def test_append_and_plus_modes_buffer_content():
    transport = _Transport()
    backend = QgaPathBackend(transport, supported_commands=FILE_COMMANDS)
    path = PosixQemuPath("/value", backend=backend)

    path.write_bytes(b"one")
    with path.open("ab") as stream:
        stream.write(b"-two")
    with path.open("r+b") as stream:
        assert isinstance(stream, io.BytesIO)
        stream.seek(0)
        stream.write(b"ONE")

    assert path.read_bytes() == b"ONE-two"


def test_seek_rpc_uses_symbolic_origin_and_validates_it():
    transport = _Transport()
    transport.files["/value"] = bytearray(b"abcdef")
    backend = QgaPathBackend(transport, supported_commands=FILE_COMMANDS)
    handle = backend._open("/value", "rb")
    try:
        assert backend.seek(handle, -2, "end", path="/value") == 4
        with pytest.raises(ValueError, match="seek origin"):
            backend.seek(handle, 0, "sideways", path="/value")
    finally:
        backend._close(handle, "/value")


def test_invalid_base64_is_mapped_and_close_does_not_mask_read_error():
    class _BrokenTransport(_Transport):
        def execute(self, request, timeout=None):
            if request["execute"] == "guest-file-read":
                return {"count": 1, "buf-b64": "%", "eof": True}
            if request["execute"] == "guest-file-close":
                super().execute(request, timeout)
                raise _QgaError("CloseFailed", "close failed")
            return super().execute(request, timeout)

    transport = _BrokenTransport()
    transport.files["/value"] = bytearray(b"x")
    backend = QgaPathBackend(transport, supported_commands=FILE_COMMANDS)

    with pytest.raises(OSError, match="invalid Base64"):
        backend.read_bytes("/value")

    assert transport.handles == {}


def test_partial_helper_reports_unsupported_operation_explicitly():
    class _StatOnly:
        def stat(self, path, *, follow_symlinks=True):
            return FileStat(st_mode=stat.S_IFREG | 0o444)

    backend = QgaPathBackend(
        _Transport(), supported_commands=FILE_COMMANDS, helper=_StatOnly()
    )
    path = PosixQemuPath("/value", backend=backend)

    assert path.is_file()
    with pytest.raises(NotImplementedError, match="does not support unlink"):
        path.unlink()


@pytest.mark.parametrize("mode", ("", "rr", "rw", "ra", "wx", "++", "r++"))
def test_open_modes_are_strict(mode):
    path = PosixQemuPath(
        "/value",
        backend=QgaPathBackend(_Transport(), supported_commands=FILE_COMMANDS),
    )

    with pytest.raises(ValueError, match="invalid mode"):
        path.open(mode)
