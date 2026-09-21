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
from ._staged_io import StagedOpenMixin, staged_open


class ContainerArchiveClient(typing.Protocol):
    """The subset of a Docker container object used by the path backend."""

    def get_archive(
        self, path: str
    ) -> typing.Tuple[typing.Iterable[bytes], typing.Mapping[str, object]]: ...

    def put_archive(self, path: str, data: bytes) -> bool: ...


class _ChunkReader(io.RawIOBase):
    """File-like adapter over Docker's chunk iterator for streaming tar reads."""

    def __init__(self, chunks: typing.Iterable[bytes]) -> None:
        self._chunks = iter(chunks)
        self._buffer = bytearray()
        self._done = False

    def readable(self) -> bool:
        return True

    def readinto(self, target: bytearray) -> int:
        while not self._buffer and not self._done:
            try:
                self._buffer.extend(next(self._chunks))
            except StopIteration:
                self._done = True
        count = min(len(target), len(self._buffer))
        target[:count] = self._buffer[:count]
        del self._buffer[:count]
        return count


class _ArchiveReadStream(io.RawIOBase):
    """Close an extracted tar member and its streaming archive together."""

    def __init__(self, member: typing.BinaryIO, archive: tarfile.TarFile) -> None:
        self._member = member
        self._archive = archive

    def readable(self) -> bool:
        return True

    def readinto(self, target: bytearray) -> int:
        data = self._member.read(len(target))
        if not data:
            return 0
        target[: len(data)] = data
        return len(data)

    def close(self) -> None:
        if not self.closed:
            try:
                self._member.close()
            finally:
                self._archive.close()
        super().close()


def _safe_name(name: str) -> typing.Tuple[str, ...]:
    """Return safe POSIX tar components, rejecting archive traversal."""
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        raise OSError(f"unsafe absolute archive member: {name!r}")
    parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
    if not parts or ".." in parts:
        raise OSError(f"unsafe archive member: {name!r}")
    return parts


#: Go's `os.FileMode` bits, as Docker sends them in the path-stat header.
#: Only the ones with a POSIX equivalent are listed; the rest (ModeAppend,
#: ModeExclusive, ModeTemporary, ModeIrregular...) have none and are dropped.
_GO_MODE_DIR = 1 << 31
_GO_MODE_SYMLINK = 1 << 27
_GO_MODE_DEVICE = 1 << 26
_GO_MODE_NAMED_PIPE = 1 << 25
_GO_MODE_SOCKET = 1 << 24
_GO_MODE_SETUID = 1 << 23
_GO_MODE_SETGID = 1 << 22
_GO_MODE_CHAR_DEVICE = 1 << 21
_GO_MODE_STICKY = 1 << 20


def _parse_rfc3339(value: str) -> int:
    """Seconds since the epoch from Docker's RFC3339Nano timestamp.

    Go emits 0-9 fractional digits with trailing zeros trimmed
    (`2026-09-20T12:34:56.7Z`), while `datetime.fromisoformat` accepts only 3
    or 6 before Python 3.11. The ValueError was mapped to 0, so on the
    declared 3.9 floor `stat().st_mtime` was epoch 0 for most container files
    -- which silently makes every mtime comparison, and so every sync
    decision, wrong.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    head, dot, rest = text.partition(".")
    if dot:
        digits = ""
        for index, char in enumerate(rest):
            if char.isdigit():
                digits += char
                continue
            rest = rest[index:]
            break
        else:
            rest = ""
        # Pad or truncate to the 6 digits every supported Python accepts.
        text = f"{head}.{(digits + '000000')[:6]}{rest}"
    try:
        return int(datetime.datetime.fromisoformat(text).timestamp())
    except ValueError:
        return 0


def _posix_mode(mode: int, *, link_target: bool = False) -> int:
    """Translate Docker's Go ``os.FileMode`` into a POSIX ``st_mode``.

    The archive stat header's ``mode`` is a Go file mode: permission bits in
    the low nine, and type in high bits that mean nothing to ``stat.S_IS*``.
    Used verbatim it left a regular file with no ``S_IFREG`` -- so
    ``is_file()`` was False for every ordinary file -- and a directory with
    ``1 << 31`` set, which ``S_ISDIR`` rejects with ``OverflowError``.

    ``_file_stat`` already does this correctly for the tar path; this is the
    same translation for the metadata path, so the two agree about one file.
    """
    result = mode & 0o777
    if mode & _GO_MODE_SETUID:
        result |= _stat.S_ISUID
    if mode & _GO_MODE_SETGID:
        result |= _stat.S_ISGID
    if mode & _GO_MODE_STICKY:
        result |= _stat.S_ISVTX

    if mode & _GO_MODE_DIR:
        return result | _stat.S_IFDIR
    if mode & _GO_MODE_SYMLINK:
        return result | _stat.S_IFLNK
    if mode & _GO_MODE_DEVICE:
        kind = _stat.S_IFCHR if mode & _GO_MODE_CHAR_DEVICE else _stat.S_IFBLK
        return result | kind
    if mode & _GO_MODE_NAMED_PIPE:
        return result | _stat.S_IFIFO
    if mode & _GO_MODE_SOCKET:
        return result | _stat.S_IFSOCK
    if link_target:
        # Older engines have been seen sending a bare permission mode with a
        # populated linkTarget; the link is the better evidence of the type.
        return result | _stat.S_IFLNK
    return result | _stat.S_IFREG


def _file_stat(member: tarfile.TarInfo) -> FileStat:
    if member.isdir():
        kind = _stat.S_IFDIR
    elif member.issym():
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

    def __init__(
        self, container: ContainerArchiveClient, *, path_flavor: str = "posix"
    ) -> None:
        self.container = container
        self.path_flavor = path_flavor

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
        # Docker returns a compact stat mapping alongside get_archive().  Use
        # it without consuming the potentially huge tar stream whenever it is
        # available; this is also the only portable way to follow symlinks.
        stream = None
        try:
            stream, metadata = self.container.get_archive(path)
            if metadata:
                value = self._metadata_stat(metadata)
                link_target = metadata.get("linkTarget")
                if follow_symlinks and link_target:
                    target = str(link_target)
                    parent = posixpath.dirname(path.rstrip("/"))
                    if not target.startswith("/"):
                        target = posixpath.join(parent, target)
                    return self._stat_following(target, hops=8)
                return value
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "response", None), "status_code", None
            )
            if status == 404:
                raise FileNotFoundError(path) from exc
            if status == 403:
                raise PermissionError(path) from exc
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        archive, members = self._archive(path)
        try:
            member = self._root_member(members)
            if follow_symlinks and member.issym():
                target = member.linkname
                parent = posixpath.dirname(path.rstrip("/"))
                target = (
                    target if target.startswith("/") else posixpath.join(parent, target)
                )
                return self._stat_following(target, hops=8)
            return _file_stat(member)
        finally:
            archive.close()

    @staticmethod
    def _metadata_stat(metadata: typing.Mapping[str, object]) -> FileStat:
        mode = _posix_mode(
            int(metadata.get("mode", 0o644)),
            link_target=bool(metadata.get("linkTarget")),
        )
        size = int(metadata.get("size", 0))
        mtime = metadata.get("mtime", 0)
        if isinstance(mtime, str):
            mtime = _parse_rfc3339(mtime)
        return FileStat(st_mode=mode, st_size=size, st_mtime=int(mtime or 0))

    def _stat_following(self, path: str, *, hops: int) -> FileStat:
        # A bounded iterative resolver avoids recursive archive calls for
        # symlink loops while retaining Docker's relative-link semantics.
        current = path
        for _ in range(hops):
            stream, metadata = self.container.get_archive(current)
            try:
                if metadata:
                    link_target = metadata.get("linkTarget")
                    if link_target:
                        target = str(link_target)
                        parent = posixpath.dirname(current.rstrip("/"))
                        current = (
                            target
                            if target.startswith("/")
                            else posixpath.join(parent, target)
                        )
                        continue
                    return self._metadata_stat(metadata)
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()
            archive, members = self._archive(current)
            try:
                member = self._root_member(members)
                if member.issym():
                    parent = posixpath.dirname(current.rstrip("/"))
                    target = member.linkname
                    current = (
                        target
                        if target.startswith("/")
                        else posixpath.join(parent, target)
                    )
                    continue
                return _file_stat(member)
            finally:
                archive.close()
        raise OSError("too many symbolic links")

    def scandir(
        self, path: str, *, hops: int = 8
    ) -> typing.List[typing.Tuple[str, FileStat]]:
        try:
            stream, _ = self.container.get_archive(path)
            archive = tarfile.open(fileobj=_ChunkReader(stream), mode="r|*")
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "response", None), "status_code", None
            )
            if status == 404:
                raise FileNotFoundError(path) from exc
            if status == 403:
                raise PermissionError(path) from exc
            raise
        try:
            root: typing.Optional[typing.Tuple[str, ...]] = None
            entries: typing.Dict[str, FileStat] = {}
            for member in archive:
                parts = _safe_name(member.name)
                if root is None:
                    root = parts
                    if member.issym():
                        # Follow it, as every other archive operation here
                        # does. This was the only one that refused, so
                        # `iterdir()` on merged-usr `/bin`, `/lib` or `/sbin`
                        # failed on every modern Linux image while `stat()`
                        # and `read_bytes()` on the same path worked.
                        if hops <= 0:
                            raise OSError("too many symbolic links")
                        target = self._link_destination(path, member.linkname)
                        return self.scandir(target, hops=hops - 1)
                    if not member.isdir():
                        raise NotADirectoryError(path)
                if parts[: len(root)] != root or len(parts) != len(root) + 1:
                    continue
                entries.setdefault(parts[-1], _file_stat(member))
            return sorted(entries.items())
        finally:
            archive.close()

    def _link_destination(self, path: str, target: str) -> str:
        """Resolve a stored link target against the link's own parent."""
        if target.startswith("/"):
            return target
        return posixpath.join(posixpath.dirname(path.rstrip("/")), target)

    def read_bytes(self, path: str, *, hops: int = 8) -> bytes:
        archive, members = self._archive(path)
        try:
            member = self._root_member(members)
            if member.isdir():
                raise IsADirectoryError(path)
            if member.issym():
                # get_archive() returns the *link* member, never the target's
                # bytes, so a read has to follow the link itself. The hop
                # budget bounds symlink loops the same way stat() does.
                if hops <= 0:
                    raise OSError("too many symbolic links")
                resolved = self._link_destination(path, member.linkname)
                archive.close()
                archive = None
                return self.read_bytes(resolved, hops=hops - 1)
            if not member.isfile() and not member.islnk():
                raise OSError(f"archive member is not a regular file: {path}")
            stream = archive.extractfile(member)
            if stream is None:
                raise OSError(f"archive member has no content: {path}")
            return stream.read()
        finally:
            if archive is not None:
                archive.close()

    def open_read(self, path: str, *, hops: int = 8) -> io.BufferedReader:
        """Open a Docker archive member as a bounded streaming reader.

        Symlinks are followed lazily -- the link is recognised from its
        already-streamed member header, so a regular file still costs one
        archive pull and the stream is never drained up front.
        """
        try:
            stream, _ = self.container.get_archive(path)
            archive = tarfile.open(fileobj=_ChunkReader(stream), mode="r|*")
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "response", None), "status_code", None
            )
            if status == 404:
                raise FileNotFoundError(path) from exc
            if status == 403:
                raise PermissionError(path) from exc
            raise
        try:
            member = archive.next()
            if member is None:
                raise FileNotFoundError(path)
            _safe_name(member.name)
            if member.isdir():
                raise IsADirectoryError(path)
            if member.issym():
                if hops <= 0:
                    raise OSError("too many symbolic links")
                resolved = self._link_destination(path, member.linkname)
                archive.close()
                return self.open_read(resolved, hops=hops - 1)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise OSError(f"archive member has no content: {path}")
            return io.BufferedReader(_ArchiveReadStream(extracted, archive))
        except BaseException:
            archive.close()
            raise

    def readlink(self, path: str) -> str:
        """Return the raw stored target of a symlink member."""
        stream = None
        try:
            stream, metadata = self.container.get_archive(path)
            if metadata:
                link_target = metadata.get("linkTarget")
                if link_target:
                    return str(link_target)
                # Metadata was authoritative and reported no link target.
                raise OSError(f"not a symbolic link: {path}")
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "response", None), "status_code", None
            )
            if status == 404:
                raise FileNotFoundError(path) from exc
            if status == 403:
                raise PermissionError(path) from exc
            if isinstance(exc, OSError):
                raise
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        archive, members = self._archive(path)
        try:
            member = self._root_member(members)
            if not member.issym():
                raise OSError(f"not a symbolic link: {path}")
            return member.linkname
        finally:
            archive.close()

    def symlink(self, path: str, target: str) -> None:
        """Create ``path`` as a symlink to ``target`` via a tar member.

        Docker's ``put_archive`` extracts tar members faithfully, and a
        ``SYMTYPE`` member is the archive representation of a symlink -- so
        unlike mkdir/unlink/rename this really is expressible through the
        archive API.
        """
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
            member.type = tarfile.SYMTYPE
            member.linkname = target
            member.size = 0
            member.mode = 0o777
            member.mtime = int(datetime.datetime.now().timestamp())
            archive.addfile(member)
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
            raise OSError(f"container rejected symlink archive for {path}")

    def _split(self, path: str) -> typing.Tuple[str, str]:
        if path.endswith(("/", "\\")):
            raise IsADirectoryError(path)
        path_module = ntpath if self.path_flavor == "windows" else posixpath
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
            try:
                prior = self.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                # 0644, as a local `open(path, "w")` yields under a default
                # umask. 0666 declared every new container file
                # world-writable.
                member.mode = 0o644
            else:
                if _stat.S_ISDIR(prior.st_mode):
                    # docker-py's put_archive omits noOverwriteDirNonDir, so
                    # moby's untar would replace the directory with this
                    # file rather than refuse.
                    raise IsADirectoryError(path)
                if _stat.S_ISLNK(prior.st_mode):
                    # A symlink's own mode is 0o777 and means nothing; taking
                    # it made the replacement file world-writable too.
                    member.mode = 0o644
                else:
                    member.mode = _stat.S_IMODE(prior.st_mode)
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


class _ContainerPathMixin(StagedOpenMixin):
    """Shared pathlib_next operations for both container path flavours."""

    __slots__ = ()

    def copy(self, target, **kwargs):
        return Path.copy(self, target, **kwargs)

    def move(self, target, **kwargs):
        return Path.move(self, target, **kwargs)

    def _copy_from(self, source, **kwargs):
        if self.exists() and not kwargs.get("overwrite", False):
            raise FileExistsError(str(self))
        with source.open("rb") as src, self.open("wb") as dst:
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)
        return self

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
        del buffering
        return staged_open(self.backend, str(self), mode, label="container")

    def symlink_to(self, target, target_is_directory: bool = False):
        """Create this path as a symlink to ``target``.

        ``target_is_directory`` exists only for
        :meth:`pathlib.Path.symlink_to` signature parity; it is a local
        Windows filesystem hint with no representation in a tar member, so
        it is accepted and ignored like every other non-local backend.
        """
        self.backend.symlink(str(self), str(target))

    def readlink(self):
        return self.with_segments(self.backend.readlink(str(self)))

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
