"""Container filesystem paths backed by Docker's archive API."""

from __future__ import annotations

import datetime
import errno
import io
import ntpath
import posixpath
import stat as _stat
import tarfile
import typing

from pathlib import PurePath as _StdPurePath
from pathapi import Path, PosixPathname, WindowsPathname
from pathapi import FileStat
from pathapi.providers import notimplemented
from ..executor.container import _status_code
from ._staged_io import copy_from, staged_open


class ContainerArchiveClient(typing.Protocol):
    """The subset of a Docker container object used by the path backend."""

    def get_archive(
        self, path: str
    ) -> typing.Tuple[typing.Iterable[bytes], typing.Mapping[str, object]]: ...

    def put_archive(self, path: str, data: bytes) -> bool: ...


class _ChunkReader(io.RawIOBase):
    """File-like adapter over Docker's chunk iterator for streaming tar reads."""

    def __init__(self, chunks: typing.Iterable[bytes]) -> None:
        self._source = chunks
        self._chunks = iter(chunks)
        self._buffer = bytearray()
        self._done = False

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        # Docker's chunk iterator IS a streaming HTTP response, and nothing
        # closed it: a `stat()` that stopped reading after the first member,
        # or a caller abandoning `open_read()`, left the connection held
        # until the generator was collected -- or forever, for a generator
        # kept alive by a traceback.
        if not self.closed:
            for candidate in (self._chunks, self._source):
                release = getattr(candidate, "close", None)
                if callable(release):
                    try:
                        release()
                    except Exception:
                        pass
        super().close()

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

    def __init__(
        self,
        member: typing.BinaryIO,
        archive: tarfile.TarFile,
        reader: typing.Optional[io.RawIOBase] = None,
    ) -> None:
        self._member = member
        self._archive = archive
        #: The chunk reader underneath, closed with everything else.
        self._reader = reader

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
                try:
                    self._archive.close()
                finally:
                    if self._reader is not None:
                        self._reader.close()
        super().close()


def _archive_oserror(exc: BaseException, path: str) -> typing.Optional[OSError]:
    """Map a Docker archive failure to a filesystem error, or `None`.

    Only 404 and 403 have a filesystem meaning; any other failure is `None`
    and propagates as the engine's own error.
    """
    status = _status_code(exc)
    if status == 404:
        return FileNotFoundError(errno.ENOENT, "no such file or directory", path)
    if status == 403:
        return PermissionError(errno.EACCES, "permission denied", path)
    return None


def _put_archive_error(
    exc: BaseException, parent: str, path: str
) -> typing.Optional[OSError]:
    """Map a failed `put_archive`: a 404 is about the parent, a 403 the path."""
    mapped = _archive_oserror(exc, parent)
    if isinstance(mapped, PermissionError):
        mapped = _archive_oserror(exc, path)
    return mapped


def _safe_name(name: str) -> typing.Tuple[str, ...]:
    """Return safe POSIX tar components, rejecting archive traversal.

    The archive ROOT (`"."` or `"./"`) is a legal member name -- it is what
    Docker sends for the container's own root -- and returns an empty tuple.
    Rejecting it as a traversal attempt made `host.path()` with no segments
    unlistable. `..` and an absolute name stay refused.
    """
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        raise OSError(f"unsafe absolute archive member: {name!r}")
    parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
    if ".." in parts:
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
        self._closed = False

    def invalidate(self) -> None:
        """Refuse further work: the host that owned this backend is closed."""
        self._closed = True

    def _engine(self) -> ContainerArchiveClient:
        if self._closed:
            raise ValueError("the container host that owns this path is closed")
        return self.container

    def stat(self, path: str, *, follow_symlinks: bool = True) -> FileStat:
        # Docker returns a compact stat mapping alongside get_archive().  Use
        # it without consuming the potentially huge tar stream whenever it is
        # available; this is also the only portable way to follow symlinks.
        stream = None
        try:
            stream, metadata = self._engine().get_archive(path)
            if metadata:
                value = self._metadata_stat(metadata)
                link_target = metadata.get("linkTarget")
                if follow_symlinks and link_target:
                    target = self._link_destination(path, str(link_target))
                    return self._stat_following(target, hops=8)
                return value
        except Exception as exc:
            mapped = _archive_oserror(exc, path)
            if mapped is not None:
                raise mapped from exc
            # Anything else propagates, as it does in `_archive`, `scandir`
            # and `open_read`. Swallowing it here meant a 500, a timeout or
            # a decode failure silently replayed the whole archive pull --
            # a second identical call, and then the tar fallback reporting
            # something else entirely.
            raise
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        archive, reader, member = self._root_stream(path)
        try:
            if follow_symlinks and member.issym():
                target = self._link_destination(path, member.linkname)
                return self._stat_following(target, hops=8)
            return _file_stat(member)
        finally:
            archive.close()
            reader.close()

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
            stream, metadata = self._engine().get_archive(current)
            try:
                if metadata:
                    link_target = metadata.get("linkTarget")
                    if link_target:
                        current = self._link_destination(current, str(link_target))
                        continue
                    return self._metadata_stat(metadata)
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()
            archive, reader, member = self._root_stream(current)
            try:
                if member.issym():
                    current = self._link_destination(current, member.linkname)
                    continue
                return _file_stat(member)
            finally:
                archive.close()
                reader.close()
        raise OSError("too many symbolic links")

    def scandir(
        self, path: str, *, hops: int = 8
    ) -> typing.List[typing.Tuple[str, FileStat]]:
        try:
            stream, _ = self._engine().get_archive(path)
            reader = _ChunkReader(stream)
            archive = tarfile.open(fileobj=reader, mode="r|*")
        except Exception as exc:
            mapped = _archive_oserror(exc, path)
            if mapped is not None:
                raise mapped from exc
            raise
        try:
            root: typing.Optional[typing.Tuple[str, ...]] = None
            entries: typing.Dict[str, FileStat] = {}
            #: Members already streamed, so a hardlink can borrow the size
            #: and mode tar stored once on its original.
            seen: typing.Dict[str, tarfile.TarInfo] = {}
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
                seen[member.name] = member
                if parts[: len(root)] != root or len(parts) != len(root) + 1:
                    continue
                entry = member
                if member.islnk():
                    # A `LNKTYPE` member carries no size or mode of its own
                    # -- tar stores those once, on the member it points at --
                    # so every hardlinked file in a listing reported size 0.
                    original = seen.get(member.linkname)
                    if original is not None:
                        entry = original
                entries.setdefault(parts[-1], _file_stat(entry))
            return sorted(entries.items())
        finally:
            archive.close()
            reader.close()

    @property
    def _path_module(self):
        r"""The path syntax this backend's container actually uses.

        `_split` already switches on the flavour; link resolution used
        `posixpath` unconditionally, so a Windows-flavoured backend joined a
        relative link target with `/` and called `C:\app\current`'s target
        absolute only if it began with `/`.
        """
        return ntpath if self.path_flavor == "windows" else posixpath

    def _link_destination(self, path: str, target: str) -> str:
        """Resolve a stored link target against the link's own parent."""
        path_module = self._path_module
        if path_module.isabs(target):
            return target
        return path_module.join(path_module.dirname(path.rstrip("/\\")), target)

    def exists(self, path: str, *, follow_symlinks: bool = True) -> bool:
        """Whether `path` exists; any failure but absence propagates."""
        try:
            self.stat(path, follow_symlinks=follow_symlinks)
        except (FileNotFoundError, NotADirectoryError):
            return False
        return True

    def _root_stream(
        self, path: str
    ) -> typing.Tuple[tarfile.TarFile, _ChunkReader, tarfile.TarInfo]:
        """Start the archive for `path` and read only its root member header.

        The caller closes the returned archive and reader. Nothing past the
        first header is pulled from the engine.
        """
        try:
            stream, _ = self._engine().get_archive(path)
            reader = _ChunkReader(stream)
            try:
                archive = tarfile.open(fileobj=reader, mode="r|*")
            except BaseException:
                reader.close()
                raise
        except Exception as exc:
            mapped = _archive_oserror(exc, path)
            if mapped is not None:
                raise mapped from exc
            raise
        try:
            member = archive.next()
            if member is None:
                raise FileNotFoundError(path)
            _safe_name(member.name)
        except BaseException:
            archive.close()
            reader.close()
            raise
        return archive, reader, member

    def read_bytes(self, path: str, *, hops: int = 8) -> bytes:
        with self.open_read(path, hops=hops) as stream:
            return stream.read()

    def open_read(self, path: str, *, hops: int = 8) -> io.BufferedReader:
        """Open a Docker archive member as a bounded streaming reader.

        Symlinks are followed lazily -- the link is recognised from its
        already-streamed member header, so a regular file still costs one
        archive pull and the stream is never drained up front.
        """
        archive, reader, member = self._root_stream(path)
        try:
            if member.isdir():
                raise IsADirectoryError(path)
            if member.issym():
                if hops <= 0:
                    raise OSError("too many symbolic links")
                resolved = self._link_destination(path, member.linkname)
                archive.close()
                reader.close()
                return self.open_read(resolved, hops=hops - 1)
            if member.islnk():
                # A single-file request answers with the link member alone;
                # its original is not in the archive, and `tarfile` raises
                # `StreamError`, outside the filesystem error vocabulary.
                raise OSError(
                    errno.ENOENT,
                    "hardlink target is outside this archive",
                    path,
                )
            extracted = archive.extractfile(member)
            if extracted is None:
                raise OSError(f"archive member has no content: {path}")
            return io.BufferedReader(_ArchiveReadStream(extracted, archive, reader))
        except BaseException:
            archive.close()
            reader.close()
            raise

    def readlink(self, path: str) -> str:
        """Return the raw stored target of a symlink member."""
        stream = None
        try:
            stream, metadata = self._engine().get_archive(path)
            if metadata:
                link_target = metadata.get("linkTarget")
                if link_target:
                    return str(link_target)
                # Metadata was authoritative and reported no link target.
                raise OSError(f"not a symbolic link: {path}")
        except Exception as exc:
            mapped = _archive_oserror(exc, path)
            if mapped is not None:
                raise mapped from exc
            if isinstance(exc, OSError):
                raise
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        archive, reader, member = self._root_stream(path)
        try:
            if not member.issym():
                raise OSError(f"not a symbolic link: {path}")
            return member.linkname
        finally:
            archive.close()
            reader.close()

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
            accepted = self._engine().put_archive(parent, payload.getvalue())
        except Exception as exc:
            mapped = _put_archive_error(exc, parent, path)
            if mapped is not None:
                raise mapped from exc
            raise
        if not accepted:
            raise OSError(f"container rejected symlink archive for {path}")

    def _split(self, path: str) -> typing.Tuple[str, str]:
        if path.endswith(("/", "\\")):
            raise IsADirectoryError(path)
        path_module = self._path_module
        parent, name = path_module.split(path_module.normpath(path))
        if not name:
            raise IsADirectoryError(path)
        return parent or ".", name

    def write_bytes(self, path: str, value: bytes, *, exclusive: bool = False) -> None:
        parent, name = self._split(path)
        member = tarfile.TarInfo(name)
        # A new file is root-owned 0644, as `docker cp` creates one (0666
        # would declare it world-writable).
        member.mode = 0o644
        if exclusive:
            try:
                self.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(path)
        else:
            self._inherit_owner(member, path)
        member.size = len(value)
        member.mtime = int(datetime.datetime.now().timestamp())
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            archive.addfile(member, io.BytesIO(value))
        try:
            accepted = self._engine().put_archive(parent, payload.getvalue())
        except Exception as exc:
            mapped = _put_archive_error(exc, parent, path)
            if mapped is not None:
                raise mapped from exc
            raise
        if not accepted:
            raise OSError(f"container rejected archive for {path}")

    def _inherit_owner(self, member: tarfile.TarInfo, path: str) -> None:
        """Give `member` the mode and owner of the file it replaces.

        The archive API takes ownership from the tar header alone, so a fresh
        header would hand an app-owned file to root. Only the first header is
        read; a missing path and a symlink (whose own mode means nothing)
        leave the defaults.
        """
        try:
            archive, reader, prior = self._root_stream(path)
        except FileNotFoundError:
            return
        try:
            if prior.isdir():
                # docker-py's put_archive omits noOverwriteDirNonDir, so
                # moby's untar would replace the directory with this file
                # rather than refuse.
                raise IsADirectoryError(path)
            if prior.issym():
                return
            member.mode = prior.mode & 0o7777
            member.uid, member.gid = prior.uid, prior.gid
            member.uname, member.gname = prior.uname, prior.gname
        finally:
            archive.close()
            reader.close()


class _ContainerPathMixin:
    """Shared pathapi operations for both container path flavours."""

    __slots__ = ()

    def copy(self, target, **kwargs):
        return Path.copy(self, target, **kwargs)

    def move(self, target, **kwargs):
        return Path.move(self, target, **kwargs)

    def _copy_from(self, source, **kwargs):
        """CPython 3.14's `Path.copy()` destination hook.

        One shared implementation (`_staged_io.copy_from`). This body used to
        be copied verbatim into four modules, and every copy diverged from
        what stdlib actually calls it with: it raised `FileExistsError` where
        stdlib overwrites, and opened a directory `"rb"`.
        """
        return copy_from(self, source, **kwargs)

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
        return staged_open(self.backend, str(self), mode)

    def _symlink_target(self, target):
        """Normalise a `str` target WITH this path's backend.

        `Path._symlink_target` builds `type(self)(target)`, and these
        classes refuse a construction without a backend -- so the public
        `symlink_to()` raised `TypeError` before reaching the primitive.
        """
        return self.with_segments(target) if isinstance(target, str) else target

    def _symlink_to(self, target, target_is_directory: bool = False):
        """Create this path as a symlink to ``target``.

        The PRIMITIVE: `pathapi`'s public wrapper supplies `force=`
        and target normalisation, which overriding the public name made
        unreachable.

        ``target_is_directory`` exists only for
        :meth:`pathlib.Path.symlink_to` signature parity; it is a local
        Windows filesystem hint with no representation in a tar member, so
        it is accepted and ignored like every other non-local backend.
        """
        self.backend.symlink(str(self), str(target))

    def readlink(self):
        return self.with_segments(self.backend.readlink(str(self)))

    # Marked absent, so `pathapi.protocols.supports()` answers `Create`,
    # `Chmod`, `Remove` and `Move` False instead of a caller having to call
    # and catch.
    @notimplemented("Docker archive APIs cannot create directories")
    def _mkdir(self, mode: int): ...

    @notimplemented("Docker archive APIs cannot change metadata")
    def chmod(self, mode: int, *, follow_symlinks: bool = True): ...

    @notimplemented("Docker archive APIs cannot remove paths")
    def unlink(self, missing_ok=False): ...

    @notimplemented("Docker archive APIs cannot remove paths")
    def rmdir(self): ...

    @notimplemented("Docker archive APIs cannot rename paths")
    def rename(self, target): ...


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
