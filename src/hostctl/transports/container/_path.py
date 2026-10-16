"""Container paths backed by the Docker archive API."""

from __future__ import annotations

import datetime
import errno
import io
import ntpath
import posixpath
import tarfile
import typing

from pathapi import FileStat
from pathapi.providers import notimplemented


from ...paths._backend import PosixBackendPath, WindowsBackendPath
from ._archive import (
    ContainerArchiveClient,
    _ArchiveReadStream,
    _ChunkReader,
    _archive_oserror,
    _file_stat,
    _parse_rfc3339,
    _posix_mode,
    _put_archive_error,
    _safe_name,
)


class ContainerPathBackend:
    """Shell-independent filesystem reads and writes through archive calls."""

    def __init__(
        self, container: ContainerArchiveClient, *, pathname: str = "posix"
    ) -> None:
        self.container = container
        self.pathname = pathname
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
        return ntpath if self.pathname == "windows" else posixpath

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
    """What differs for container paths: the Docker archive API cannot mutate."""

    __slots__ = ()

    def _symlink_to(self, target, target_is_directory: bool = False):
        """Create this path as a symlink to ``target``.

        ``target_is_directory`` exists only for signature parity; a tar
        member has no representation of it, so it is accepted and ignored.
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


class PosixContainerPath(_ContainerPathMixin, PosixBackendPath):
    """A POSIX container path backed by Docker archive operations."""

    __slots__ = ()
    _backend_type = ContainerPathBackend


class WindowsContainerPath(_ContainerPathMixin, WindowsBackendPath):
    """A Windows container path backed by Docker archive operations."""

    __slots__ = ()
    _backend_type = ContainerPathBackend
