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


def _tar_entries(entries):
    """Build a tar from (name, type, payload, linkname) tuples."""
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name, kind, value, linkname in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind == tarfile.SYMTYPE:
                member.linkname = linkname
                archive.addfile(member)
            elif kind == tarfile.DIRTYPE:
                member.mode = 0o755
                archive.addfile(member)
            elif kind == tarfile.LNKTYPE:
                # A hardlink member: tar stores the size and mode once, on
                # the original, and this member carries neither.
                member.linkname = linkname
                archive.addfile(member)
            else:
                member.size = len(value)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(value))
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


def _sent_member(container):
    """The single tar member the backend uploaded."""
    _parent, data = container.puts[-1]
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = archive.getmembers()
    assert len(members) == 1
    return members[0]


def test_container_stat_closes_unused_archive_stream():
    class Stream:
        closed = False

        def close(self):
            self.closed = True

    stream = Stream()
    container = _Container()
    container.get_archive = lambda path: (
        stream,
        {"name": "data", "size": 4, "mode": 0o644, "mtime": "0"},
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
                {"name": "target", "size": 4, "mode": 0o644, "mtime": "0"},
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


def test_engine_file_modes_are_translated_from_go_to_posix():
    """Docker's path-stat header carries a Go os.FileMode, not a POSIX one.

    Used verbatim, a regular file had no S_IFREG -- `is_file()` was False for
    every ordinary file -- and a directory carried `1 << 31`, which `S_ISDIR`
    rejects with OverflowError.
    """
    import stat

    cases = {
        # (go mode, linkTarget) -> predicate that must be true
        (493, ""): stat.S_ISREG,  # docker-py's own documented /bin/sh header
        ((1 << 31) | 0o755, ""): stat.S_ISDIR,
        ((1 << 27) | 0o777, ""): stat.S_ISLNK,
        ((1 << 25) | 0o644, ""): stat.S_ISFIFO,
        ((1 << 24) | 0o755, ""): stat.S_ISSOCK,
        ((1 << 26) | (1 << 21) | 0o666, ""): stat.S_ISCHR,
        ((1 << 26) | 0o660, ""): stat.S_ISBLK,
        (0o777, "elsewhere"): stat.S_ISLNK,
    }
    for (mode, link_target), predicate in cases.items():
        container = _Container()
        container.get_archive = lambda path, mode=mode, link_target=link_target: (
            io.BytesIO(),
            {
                "name": "x",
                "size": 1,
                "mode": mode,
                "mtime": "0",
                "linkTarget": link_target,
            },
        )

        result = ContainerPathBackend(container).stat("/x", follow_symlinks=False)

        assert predicate(result.st_mode), (oct(mode), oct(result.st_mode))

    container = _Container()
    container.get_archive = lambda path: (
        io.BytesIO(),
        {"name": "x", "size": 1, "mode": (1 << 23) | 0o755, "mtime": "0"},
    )
    mode = ContainerPathBackend(container).stat("/x").st_mode
    assert stat.S_IMODE(mode) == 0o4755


@pytest.mark.parametrize(
    ("value", "expected_utc"),
    (
        ("2026-09-20T12:34:56Z", "2026-09-20T12:34:56+00:00"),
        ("2026-09-20T12:34:56.7Z", "2026-09-20T12:34:56+00:00"),
        ("2026-09-20T12:34:56.123456789Z", "2026-09-20T12:34:56+00:00"),
        ("2026-09-20T12:34:56.123456Z", "2026-09-20T12:34:56+00:00"),
    ),
)
def test_rfc3339nano_timestamps_parse_on_every_supported_python(value, expected_utc):
    """Go trims trailing zeros, so it emits 0-9 fractional digits.

    `datetime.fromisoformat` accepts only 3 or 6 before 3.11, and the
    ValueError was mapped to 0 -- so on the declared 3.9 floor `st_mtime` was
    epoch 0 for most container files, quietly wrecking every mtime comparison.
    """
    import datetime

    from hostctl.host.container_path import _parse_rfc3339

    parsed = _parse_rfc3339(value)

    assert parsed > 0
    assert (
        datetime.datetime.fromtimestamp(parsed, datetime.timezone.utc).isoformat()
        == expected_utc
    )


def test_an_unparseable_timestamp_is_still_zero():
    from hostctl.host.container_path import _parse_rfc3339

    assert _parse_rfc3339("not a timestamp") == 0


def test_a_new_file_is_not_world_writable():
    """0666 declared every new container file world-writable; a local
    `open(path, "w")` yields 0644 under a default umask."""
    container = _Container()
    container.get_archive = lambda path: (_ for _ in ()).throw(FileNotFoundError(path))

    ContainerPathBackend(container).write_bytes("/srv/new.txt", b"data")

    member = _sent_member(container)
    assert member.mode == 0o644


def test_writing_over_a_directory_raises_instead_of_replacing_it():
    """docker-py's put_archive omits noOverwriteDirNonDir, so moby's untar
    would replace the directory with a regular file."""
    container = _Container()
    container.get_archive = lambda path: (
        io.BytesIO(),
        {"name": "srv", "size": 0, "mode": (1 << 31) | 0o755, "mtime": "0"},
    )

    with pytest.raises(IsADirectoryError):
        ContainerPathBackend(container).write_bytes("/srv", b"data")

    assert container.puts == []


def test_writing_over_a_symlink_does_not_inherit_its_mode():
    """A symlink's own mode is 0o777 and means nothing."""
    container = _Container()
    container.get_archive = lambda path: (
        io.BytesIO(),
        {
            "name": "link",
            "size": 0,
            "mode": (1 << 27) | 0o777,
            "mtime": "0",
            "linkTarget": "/elsewhere",
        },
    )

    ContainerPathBackend(container).write_bytes("/srv/link", b"data")

    assert _sent_member(container).mode == 0o644


def test_iterdir_follows_a_symlinked_directory():
    """merged-usr `/bin`, `/lib` and `/sbin` are symlinks on every modern image.

    scandir() was the only archive operation that refused to follow one, so
    `iterdir('/bin')` raised NotADirectoryError while `stat()` and
    `read_bytes()` on the same path followed it correctly.
    """
    container = _Container()
    container.archives["/bin"] = _tar_entries(
        [("bin", tarfile.SYMTYPE, b"", "usr/bin")]
    )
    container.archives["/usr/bin"] = _tar_entries(
        [("bin", tarfile.DIRTYPE, b"", None), ("bin/ls", tarfile.REGTYPE, b"x", None)]
    )

    names = [name for name, _stat in ContainerPathBackend(container).scandir("/bin")]

    assert names == ["ls"]


def test_a_symlink_loop_in_scandir_is_bounded():
    container = _Container()
    container.archives["/a"] = _tar_entries([("a", tarfile.SYMTYPE, b"", "/b")])
    container.archives["/b"] = _tar_entries([("b", tarfile.SYMTYPE, b"", "/a")])

    with pytest.raises(OSError, match="too many symbolic links"):
        ContainerPathBackend(container).scandir("/a")


def test_stat_parses_the_timestamp_real_docker_sends():
    """The Engine API marshals `Mtime` as Go's `time.RFC3339Nano`, and that
    string branch is the one production always takes -- yet every test fed
    the parser the literal "0", which is not RFC3339 and lands in the error
    fallback, while the conformance fake sent an int. So the branch Docker
    exercises on every stat was covered by nothing, and on the 3.9 floor it
    yielded epoch 0: every mtime comparison, and so every sync decision,
    silently wrong."""
    container = _Container()
    container.get_archive = lambda path: (
        iter((b"",)),
        {
            "name": "data",
            "size": 4,
            "mode": 0o644,
            "mtime": "2026-09-20T12:34:56.123456789Z",
        },
    )

    result = ContainerPathBackend(container).stat("/data")

    assert result.st_mtime == 1789907696  # 2026-09-20T12:34:56Z


def test_a_streaming_archive_pull_closes_the_http_response():
    """Docker's chunk iterator IS a streaming HTTP response, and nothing
    closed it: a caller abandoning `open_read()` -- or a `scandir` that
    stopped reading after the first member -- held the connection until the
    generator was collected, or forever behind a traceback."""
    closed = []

    class _Stream:
        def __init__(self, payload):
            self._chunks = iter([payload])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._chunks)

        def close(self):
            closed.append(True)

    container = _Container()
    payload = _tar([("data.bin", b"value")])
    container.get_archive = lambda path: (_Stream(payload), {})

    backend = ContainerPathBackend(container)
    with backend.open_read("/data.bin") as stream:
        assert stream.read() == b"value"

    # Closed exactly once per pull, whichever handle got there first.
    assert closed


def test_a_composed_archive_provider_can_reach_symlink_operations():
    """The provider implements `symlink_to`/`readlink` -- a `SYMTYPE` tar
    member is a faithful representation -- and did not declare them, so a
    composed host refused operations its backend performs."""
    from hostctl import PosixHost
    from hostctl.provider.transports import ContainerArchivePathProvider

    container = _Container()
    container.archives["/links/link"] = _tar_entries(
        [("link", tarfile.SYMTYPE, None, "target")]
    )
    backend = ContainerPathBackend(container)
    host = PosixHost(
        path_providers=(
            ContainerArchivePathProvider(
                lambda *parts: PosixContainerPath(*parts, backend=backend)
            ),
        )
    )

    assert str(host.path("/links/link").readlink()).endswith("target")


def test_a_hardlinked_entry_reports_the_size_tar_stored_once():
    """A `LNKTYPE` member carries no size or mode of its own -- tar stores
    those on the member it points at -- so every hardlinked file in a
    listing reported size 0."""
    container = _Container()
    container.archives["/links"] = _tar_entries(
        [
            ("links", tarfile.DIRTYPE, None, None),
            ("links/target", tarfile.REGTYPE, b"payload", None),
            ("links/hard-link", tarfile.LNKTYPE, None, "links/target"),
        ]
    )

    entries = dict(ContainerPathBackend(container).scandir("/links"))

    assert entries["hard-link"].st_size == len(b"payload")


def test_the_archive_root_is_a_legal_member_name():
    """`"."` is what Docker sends for the container's own root, and
    rejecting it as a traversal attempt made `host.path()` with no segments
    unlistable."""
    from hostctl.host.container_path import _safe_name

    assert _safe_name(".") == ()
    assert _safe_name("./") == ()
    with pytest.raises(OSError):
        _safe_name("../escape")


def test_a_single_member_hardlink_archive_is_an_oserror_not_a_tar_error():
    """The only hardlink test served a three-member archive whose root
    `_root_member` resolved to the regular file, so the `LNKTYPE` branch was
    never reached -- and Docker's single-file request answers with the link
    member alone, where `tarfile` raises `StreamError`, outside the
    filesystem error vocabulary entirely."""
    container = _Container()
    container.archives["/links/hard-link"] = _tar_entries(
        [("hard-link", tarfile.LNKTYPE, None, "target")]
    )

    with pytest.raises(OSError) as raised:
        ContainerPathBackend(container).read_bytes("/links/hard-link")

    assert not isinstance(raised.value, tarfile.TarError)
