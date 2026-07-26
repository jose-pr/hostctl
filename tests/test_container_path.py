"""Container archive path tests using a Docker-shaped fake client."""

import io
import stat
import tarfile

import pytest
from pathlib_next import Path

from hostctl.host.container_path import (
    ContainerPathBackend,
    PosixContainerPath,
    WindowsContainerPath,
)


def _tar(entries):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name, value in entries:
            member = tarfile.TarInfo(name)
            if value is None:
                member.type = tarfile.DIRTYPE
                member.mode = 0o755
            else:
                member.size = len(value)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(value))
                continue
            archive.addfile(member)
    return payload.getvalue()


class _Container:
    def __init__(self):
        self.archives = {}
        self.puts = []

    def get_archive(self, path):
        try:
            payload = self.archives[path]
        except KeyError:
            error = RuntimeError(path)
            error.status_code = 404
            raise error
        return iter((payload[:7], payload[7:])), {}

    def put_archive(self, path, data):
        self.puts.append((path, data))
        return True


def test_posix_path_contract_backend_propagation_and_archive_reads():
    container = _Container()
    container.archives["/srv"] = _tar(
        (("srv", None), ("srv/a.txt", b"a"), ("srv/sub", None), ("srv/sub/x", b"x"))
    )
    container.archives["/srv/a.txt"] = _tar((("a.txt", b"hello"),))
    backend = ContainerPathBackend(container)
    root = PosixContainerPath("/srv", backend=backend)

    assert isinstance(root, Path)
    assert root.parent.backend is backend
    assert (root / "a.txt").backend is backend
    assert sorted(path.name for path in root.iterdir()) == ["a.txt", "sub"]
    assert (root / "a.txt").read_bytes() == b"hello"
    assert stat.S_ISREG((root / "a.txt").stat().st_mode)


def test_windows_path_semantics_and_backend_propagation():
    backend = ContainerPathBackend(_Container())
    path = WindowsContainerPath(r"C:\Temp\file.txt", backend=backend)

    assert isinstance(path, Path)
    assert path.drive == "C:"
    assert str(path.parent) == r"C:\Temp"
    assert path.parent.backend is backend
    assert (path.parent / "other").backend is backend


def test_write_append_and_exclusive_archives_only_the_basename():
    container = _Container()
    container.archives["/work/data.bin"] = _tar((("data.bin", b"old"),))
    backend = ContainerPathBackend(container)
    path = PosixContainerPath("/work/data.bin", backend=backend)

    path.write_bytes(b"new")
    with path.open("ab") as stream:
        stream.write(b"!")
    with pytest.raises(FileExistsError):
        path.open("xb").close()

    assert [parent for parent, _ in container.puts] == ["/work", "/work"]
    with tarfile.open(fileobj=io.BytesIO(container.puts[-1][1])) as archive:
        member = archive.getmembers()[0]
        assert member.name == "data.bin"
        assert archive.extractfile(member).read() == b"old!"


def test_archive_member_traversal_is_rejected():
    container = _Container()
    container.archives["/bad"] = _tar((("../escape", b"x"),))
    path = PosixContainerPath("/bad", backend=ContainerPathBackend(container))

    with pytest.raises(OSError, match="unsafe archive member"):
        path.read_bytes()


@pytest.mark.parametrize("operation", ("mkdir", "unlink", "rmdir", "rename", "chmod"))
def test_unsupported_archive_mutations_are_explicit(operation):
    backend = ContainerPathBackend(_Container())
    path = PosixContainerPath("/value", backend=backend)

    with pytest.raises(NotImplementedError):
        if operation == "mkdir":
            path.mkdir()
        elif operation == "unlink":
            path.unlink()
        elif operation == "rmdir":
            path.rmdir()
        elif operation == "rename":
            path.rename("/other")
        else:
            path.chmod(0o600)
