"""Container filesystem paths backed by Docker's archive API."""

from __future__ import annotations

import datetime
import io
import ntpath
import posixpath
import stat as _stat
import tarfile
import typing

from pathlib import PurePath as _StdPurePath
from pathlib_next import Path, PosixPathname, WindowsPathname
from pathlib_next.utils.stat import FileStat


class ContainerArchiveClient(typing.Protocol):
    """The subset of a Docker container object used by the path backend."""

    def get_archive(
        self, path: str
    ) -> typing.Tuple[typing.Iterable[bytes], typing.Mapping[str, object]]: ...

    def put_archive(self, path: str, data: bytes) -> bool: ...


def _safe_name(name: str) -> typing.Tuple[str, ...]:
    """Return safe POSIX tar components, rejecting archive traversal."""
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        raise OSError(f"unsafe absolute archive member: {name!r}")
    parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
    if not parts or ".." in parts:
        raise OSError(f"unsafe archive member: {name!r}")
    return parts


def _file_stat(member: tarfile.TarInfo) -> FileStat:
    if member.isdir():
        kind = _stat.S_IFDIR
    elif member.issym() or member.islnk():
        kind = _stat.S_IFLNK
    elif member.ischr():
        kind = _stat.S_IFCHR
    elif member.isblk():
        kind = _stat.S_IFBLK
    elif member.isfifo():
        kind = _stat.S_IFIFO
    else:
        kind = _stat.S_IFREG
    return FileStat(
        st_mode=kind | member.mode,
        st_size=member.size,
        st_mtime=int(member.mtime),
    )


class ContainerPathBackend:
    """Shell-independent filesystem reads and writes through archive calls."""

    def __init__(self, container: ContainerArchiveClient) -> None:
        self.container = container

    def _archive(
        self, path: str
    ) -> typing.Tuple[tarfile.TarFile, typing.List[tarfile.TarInfo]]:
        try:
            stream, _ = self.container.get_archive(path)
            payload = b"".join(stream)
        except Exception as exc:
            # Docker SDK/API errors are optional dependency types. Avoid an
            # import here while still exposing ordinary filesystem failures.
            status = getattr(exc, "status_code", None)
            response = getattr(exc, "response", None)
            status = status or getattr(response, "status_code", None)
            if status == 404:
                raise FileNotFoundError(path) from exc
            if status == 403:
                raise PermissionError(path) from exc
            raise
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:*")
        members = archive.getmembers()
        for member in members:
            _safe_name(member.name)
            if member.linkname:
                _safe_name(member.linkname)
        if not members:
            archive.close()
            raise FileNotFoundError(path)
        return archive, members

    @staticmethod
    def _root_member(
        members: typing.Sequence[tarfile.TarInfo],
    ) -> tarfile.TarInfo:
        return min(members, key=lambda member: len(_safe_name(member.name)))

    def stat(self, path: str, *, follow_symlinks: bool = True) -> FileStat:
        archive, members = self._archive(path)
        try:
            member = self._root_member(members)
            if follow_symlinks and (member.issym() or member.islnk()):
                raise NotImplementedError(
                    "ContainerPath cannot follow archive symlinks portably"
                )
            return _file_stat(member)
        finally:
            archive.close()

    def scandir(self, path: str) -> typing.List[typing.Tuple[str, FileStat]]:
        archive, members = self._archive(path)
        try:
            root = _safe_name(self._root_member(members).name)
            entries: typing.Dict[str, FileStat] = {}
            for member in members:
                parts = _safe_name(member.name)
                if parts[: len(root)] != root or len(parts) != len(root) + 1:
                    continue
                entries.setdefault(parts[-1], _file_stat(member))
            return sorted(entries.items())
        finally:
            archive.close()

    def read_bytes(self, path: str) -> bytes:
        archive, members = self._archive(path)
        try:
            member = self._root_member(members)
            if member.isdir():
                raise IsADirectoryError(path)
            if not member.isfile():
                raise OSError(f"archive member is not a regular file: {path}")
            stream = archive.extractfile(member)
            if stream is None:
                raise OSError(f"archive member has no content: {path}")
            return stream.read()
        finally:
            archive.close()

    @staticmethod
    def _split(path: str) -> typing.Tuple[str, str]:
        path_module = (
            ntpath if "\\" in path or ntpath.splitdrive(path)[0] else posixpath
        )
        parent, name = path_module.split(path_module.normpath(path))
        if not name:
            raise IsADirectoryError(path)
        return parent or ".", name

    def write_bytes(self, path: str, value: bytes, *, exclusive: bool = False) -> None:
        if exclusive:
            try:
                self.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(path)
        parent, name = self._split(path)
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            member = tarfile.TarInfo(name)
            member.size = len(value)
            member.mode = 0o666
            member.mtime = int(datetime.datetime.now().timestamp())
            archive.addfile(member, io.BytesIO(value))
        try:
            accepted = self.container.put_archive(parent, payload.getvalue())
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            response = getattr(exc, "response", None)
            status = status or getattr(response, "status_code", None)
            if status == 404:
                raise FileNotFoundError(parent) from exc
            if status == 403:
                raise PermissionError(path) from exc
            raise
        if not accepted:
            raise OSError(f"container rejected archive for {path}")


class _WriteBackBytesIO(io.BytesIO):
    def __init__(
        self, value: bytes, commit: typing.Optional[typing.Callable[[bytes], None]]
    ) -> None:
        super().__init__(value)
        self._commit = commit

    def close(self) -> None:
        if not self.closed and self._commit is not None:
            value = self.getvalue()
            commit, self._commit = self._commit, None
            try:
                commit(value)
            finally:
                super().close()
        else:
            super().close()


class _ContainerPathMixin:
    """Shared pathlib_next operations for both container path flavours."""

    __slots__ = ()

    @property
    def backend(self) -> ContainerPathBackend:
        return self._backend

    def with_segments(self, *segments: str):
        return type(self)(*segments, backend=self.backend)

    def __truediv__(self, key):
        return type(self)(self, key, backend=self.backend)

    def joinpath(self, *args):
        return type(self)(self, *args, backend=self.backend)

    @property
    def parent(self):
        return type(self)(str(super().parent), backend=self.backend)

    def stat(self, *, follow_symlinks: bool = True) -> FileStat:
        return self.backend.stat(str(self), follow_symlinks=follow_symlinks)

    def _scandir(self):
        yield from self.backend.scandir(str(self))

    def iterdir(self):
        for name, _ in self._scandir():
            yield self / name

    def _open(self, mode="r", buffering=-1):
        if (
            not mode
            or sum(mode.count(value) for value in "rwax") != 1
            or mode.count("+") > 1
            or len(mode) != 1 + mode.count("+")
        ):
            raise ValueError(f"invalid mode: {mode!r}")
        readable = "r" in mode or "+" in mode
        writable = any(value in mode for value in "wax+")
        if "r" in mode or "a" in mode:
            try:
                value = self.backend.read_bytes(str(self))
            except FileNotFoundError:
                if "a" in mode:
                    value = b""
                else:
                    raise
        else:
            value = b""
        stream = _WriteBackBytesIO(
            value,
            (
                (
                    lambda data: self.backend.write_bytes(
                        str(self), data, exclusive="x" in mode
                    )
                )
                if writable
                else None
            ),
        )
        if "a" in mode:
            stream.seek(0, io.SEEK_END)
        elif not readable:
            stream.seek(0)
        return stream

    def _mkdir(self, mode: int):
        raise NotImplementedError("Docker archive APIs cannot create directories")

    def chmod(self, mode: int, *, follow_symlinks: bool = True):
        raise NotImplementedError("Docker archive APIs cannot change metadata")

    def unlink(self, missing_ok=False):
        raise NotImplementedError("Docker archive APIs cannot remove paths")

    def rmdir(self):
        raise NotImplementedError("Docker archive APIs cannot remove paths")

    def rename(self, target):
        raise NotImplementedError("Docker archive APIs cannot rename paths")


class PosixContainerPath(_ContainerPathMixin, PosixPathname, Path):
    """A POSIX container path backed by Docker archive operations."""

    __slots__ = ("_backend",)

    def __init__(self, *segments, backend=None):
        # Python 3.14's pathlib.PurePath.__init__ no longer accepts kwargs.
        # Path state is initialized by __new__; backend is attached there.
        if not hasattr(self, "_raw_paths") and not hasattr(self, "_parts"):
            _StdPurePath.__init__(self, *segments)

    def __new__(
        cls,
        *segments: typing.Union[str, PosixPathname],
        backend: typing.Optional[ContainerPathBackend] = None,
    ):
        inherited = next(
            (
                segment.backend
                for segment in segments
                if isinstance(segment, _ContainerPathMixin)
            ),
            None,
        )
        self = super().__new__(cls, *segments)
        self._backend = backend or inherited
        if self._backend is None:
            raise TypeError("PosixContainerPath requires a backend")
        return self


class WindowsContainerPath(_ContainerPathMixin, WindowsPathname, Path):
    """A Windows container path backed by Docker archive operations."""

    __slots__ = ("_backend",)

    def __init__(self, *segments, backend=None):
        # Python 3.14's pathlib.PurePath.__init__ no longer accepts kwargs.
        # Path state is initialized by __new__; backend is attached there.
        if not hasattr(self, "_raw_paths") and not hasattr(self, "_parts"):
            _StdPurePath.__init__(self, *segments)

    def __new__(
        cls,
        *segments: typing.Union[str, WindowsPathname],
        backend: typing.Optional[ContainerPathBackend] = None,
    ):
        inherited = next(
            (
                segment.backend
                for segment in segments
                if isinstance(segment, _ContainerPathMixin)
            ),
            None,
        )
        self = super().__new__(cls, *segments)
        self._backend = backend or inherited
        if self._backend is None:
            raise TypeError("WindowsContainerPath requires a backend")
        return self
