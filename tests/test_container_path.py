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


def _tar_special():
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        target = tarfile.TarInfo("target")
        target.mode = 0o640
        target.size = 4
        archive.addfile(target, io.BytesIO(b"data"))
        symlink = tarfile.TarInfo("absolute-link")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "/etc/hosts"
        archive.addfile(symlink)
        hardlink = tarfile.TarInfo("hard-link")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "target"
        archive.addfile(hardlink)
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


def test_container_stat_closes_unused_archive_stream():
    class Stream:
        closed = False

        def close(self):
            self.closed = True

    stream = Stream()
    container = _Container()
    container.get_archive = lambda path: (
        stream,
        {"name": "data", "size": 4, "mode": 0o100644, "mtime": "0"},
    )

    result = ContainerPathBackend(container).stat("/data")

    assert result.st_size == 4
    assert stream.closed


def test_container_followed_metadata_links_close_every_unused_stream():
    class Stream:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    streams = [Stream(), Stream()]
    responses = iter(
        (
            (streams[0], {"linkTarget": "target"}),
            (
                streams[1],
                {"name": "target", "size": 4, "mode": 0o100644, "mtime": "0"},
            ),
        )
    )
    container = _Container()
    container.get_archive = lambda path: next(responses)

    result = ContainerPathBackend(container).stat("/link", follow_symlinks=True)

    assert result.st_size == 4
    assert all(stream.closed for stream in streams)


def test_container_open_read_is_lazy_and_bounded():
    container = _Container()
    payload = _tar((("data.bin", b"x" * (1024 * 1024)),))
    pulls = []

    def get_archive(path):
        if path != "/data.bin":
            raise FileNotFoundError(path)

        def chunks():
            for offset in range(0, len(payload), 1024):
                pulls.append(offset)
                yield payload[offset : offset + 1024]

        return chunks(), {}

    container.get_archive = get_archive
    path = PosixContainerPath("/data.bin", backend=ContainerPathBackend(container))
    with path.open("rb") as stream:
        assert stream.read(1) == b"x"
        assert len(pulls) < len(payload) // 1024


def test_container_path_copy_to_local_path(tmp_path):
    container = _Container()
    container.archives["/data.bin"] = _tar((("data.bin", b"cross-host"),))
    source = PosixContainerPath(
        "/data.bin",
        backend=ContainerPathBackend(container),
    )
    target = Path(tmp_path / "copy.bin")
    source.copy(target)
    assert target.read_bytes() == b"cross-host"
    with pytest.raises(FileExistsError):
        source.copy(target)


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


def test_archive_links_keep_targets_unvalidated_and_hardlinks_are_files():
    container = _Container()
    container.archives["/links"] = _tar_special()
    container.archives["/links/absolute-link"] = _tar((("absolute-link", None),))
    # Replace the directory-style entry with a symlink member.
    link_payload = io.BytesIO()
    with tarfile.open(fileobj=link_payload, mode="w") as archive:
        member = tarfile.TarInfo("absolute-link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/hosts"
        archive.addfile(member)
    container.archives["/links/absolute-link"] = link_payload.getvalue()
    container.archives["/links/hard-link"] = _tar_special()
    container.archives["/etc/hosts"] = _tar((("hosts", b"hosts"),))
    backend = ContainerPathBackend(container)
    root = PosixContainerPath("/links", backend=backend)

    assert stat.S_ISLNK((root / "absolute-link").stat(follow_symlinks=False).st_mode)
    assert (root / "hard-link").read_bytes() == b"data"
    assert stat.S_ISREG((root / "hard-link").stat().st_mode)


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
