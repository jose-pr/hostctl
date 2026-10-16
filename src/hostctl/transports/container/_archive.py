"""Docker archive transfers: tar streams, error mapping and file metadata."""

from __future__ import annotations

import datetime

import errno

import io


import stat as _stat

import tarfile

import typing

from pathapi import FileStat


from ._executor import _status_code


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
