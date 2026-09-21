"""Checksum and progress helpers for :mod:`pathlib_next` copy and sync."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import typing

from pathlib_next.utils.sync import PathAndStat

if typing.TYPE_CHECKING:
    from .host import Host


_REMOTE_ALGORITHMS = frozenset(("md5", "sha1", "sha256", "sha384", "sha512"))
_HEX_DIGEST = re.compile(r"^[0-9a-fA-F]+$")

#: Remote hashing is an optimization, never a requirement.  A host which owns
#: the path but cannot hash it in place -- no such tool, a refused command, an
#: unparseable answer -- must degrade to reading the content, not fail the
#: whole sync.
#:
#: `OSError` IS absorbed here, deliberately: a remote hash reports a missing
#: file the same way it reports a missing tool, and telling them apart at
#: this layer means parsing a shell's stderr. The streaming fallback opens
#: the path, so a genuinely missing file raises there instead -- one line
#: later, from the layer that can tell.
_UNAVAILABLE_REMOTE_TOOL = (
    ValueError,
    OSError,
    NotImplementedError,
    subprocess.SubprocessError,
)


def stat_checksum(entry: PathAndStat) -> tuple[int, float]:
    """Return the cached size and modification time without reading content.

    This is rsync's quick check: no content is read and no command is run.
    Two caveats decide whether it suits a given sync.

    It can *miss* a change which preserves both size and modification time.

    It can also *report* a change which is not one: ``Path.copy()``
    propagates ``st_mode`` and not timestamps, so a file this helper copies
    lands with a fresh modification time and compares unequal on the next
    run -- the sync never settles.  Measured on both supported interpreters
    with ``pathlib_next`` 0.9.10: a backdated source and its fresh copy
    differ by the full hour on 3.14 as well as on 3.9.  (CPython 3.14 added
    its own ``Path.copy()`` which does preserve timestamps, but
    ``pathlib_next`` routes explicitly around it to keep one cross-version
    contract, so it is not reached.)

    So: use this helper where source modification times are meaningful on
    both sides -- a tree replicated by something which preserves them -- and
    :func:`host_checksum` when hostctl's own copies must converge to a no-op.
    """
    if entry.stat is None:
        raise FileNotFoundError(entry.path)
    return entry.stat.st_size, entry.stat.st_mtime


def host_checksum(
    *hosts: Host,
    algorithm: str = "md5",
    chunk_size: int = 1024 * 1024,
) -> typing.Callable[[PathAndStat], str]:
    """Build a ``PathSyncer`` checksum using execution beside owned paths.

    More than one host may be supplied for a cross-host sync. Paths which do
    not belong to any supplied host are hashed through their binary ``open()``
    contract, preserving interoperability with arbitrary ``pathlib_next``
    implementations.
    """
    normalized = algorithm.casefold().replace("-", "")
    if normalized not in _REMOTE_ALGORITHMS:
        raise ValueError(
            f"unsupported remote checksum algorithm: {algorithm!r}; "
            f"choose one of {', '.join(sorted(_REMOTE_ALGORITHMS))}"
        )
    if not hosts:
        raise ValueError("host_checksum requires at least one host")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    owners = tuple((host, _host_path_token(host)) for host in hosts)

    def checksum(entry: PathAndStat) -> str:
        for host, owner_token in owners:
            if _host_owns_path(host, entry.path, owner_token):
                if _is_local_owner(host, entry.path):
                    # A local file needs no process. Spawning `md5sum` per
                    # file cost a fork, an exec and a shell parse to hash
                    # bytes this process can read directly -- and on Windows
                    # it went through certutil, which is slower still.
                    return _stream_checksum(entry.path, normalized, chunk_size)
                try:
                    return _remote_checksum(host, entry.path, normalized)
                except _UNAVAILABLE_REMOTE_TOOL:
                    # The host owns the path but cannot hash it in place: the
                    # tool is missing, refused, or answered unintelligibly.
                    # Reading the content is slower but still correct, so a
                    # sync degrades rather than aborting.
                    break
        return _stream_checksum(entry.path, normalized, chunk_size)

    return checksum


class ProgressReader:
    """Wrap a binary reader and report ``(bytes_read, total_bytes)``."""

    def __init__(
        self,
        reader: typing.BinaryIO,
        callback: typing.Callable[[int, typing.Optional[int]], None],
        *,
        total: typing.Optional[int] = None,
    ) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        self.reader = reader
        self.callback = callback
        self.total = total
        self.bytes_read = 0

    def _count(self, read: int) -> None:
        self.bytes_read += read
        self.callback(self.bytes_read, self.total)

    def read(self, size: int = -1) -> bytes:
        data = self.reader.read(size)
        self._count(len(data))
        return data

    # Every other way to read. Only `read()` used to count -- the rest went
    # through `__getattr__` uncounted, and the dunders bypass it entirely --
    # so `hashlib.file_digest`, a `BufferedReader` wrapper, and any
    # line-based consumer hashed or copied a whole file and reported no
    # progress at all.
    def read1(self, size: int = -1) -> bytes:
        read1 = getattr(self.reader, "read1", None)
        data = read1(size) if callable(read1) else self.reader.read(size)
        self._count(len(data))
        return data

    def readinto(self, buffer) -> int:
        readinto = getattr(self.reader, "readinto", None)
        if callable(readinto):
            read = readinto(buffer)
        else:
            data = self.reader.read(len(buffer))
            read = len(data)
            buffer[:read] = data
        self._count(read or 0)
        return read or 0

    def readinto1(self, buffer) -> int:
        readinto1 = getattr(self.reader, "readinto1", None)
        if not callable(readinto1):
            return self.readinto(buffer)
        read = readinto1(buffer)
        self._count(read or 0)
        return read or 0

    def readline(self, size: int = -1) -> bytes:
        data = self.reader.readline(size)
        self._count(len(data))
        return data

    def readlines(self, hint: int = -1) -> "list[bytes]":
        lines = self.reader.readlines(hint)
        self._count(sum(len(line) for line in lines))
        return lines

    def __iter__(self) -> "ProgressReader":
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def readable(self) -> bool:
        return True

    @property
    def closed(self) -> bool:
        return self.reader.closed

    def close(self) -> None:
        self.reader.close()

    def __enter__(self) -> ProgressReader:
        self.reader.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> typing.Optional[bool]:
        return self.reader.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name: str) -> object:
        return getattr(self.reader, name)


def _remote_checksum(host: Host, path: object, algorithm: str) -> str:
    flavour = host.shell_flavour.name
    if flavour in ("powershell", "pwsh"):
        quote = host.shell_flavour.quote
        script = (
            f"Get-FileHash -LiteralPath {quote(os.fspath(path))} "
            f"-Algorithm {quote(algorithm.upper())} "
            "| Select-Object -ExpandProperty Hash"
        )
        result = host.run(script, encoding="utf-8")
        try:
            return _parse_digest(result.stdout, algorithm)
        except ValueError:
            result = host.run(
                ("certutil.exe", "-hashfile", os.fspath(path), algorithm.upper()),
                encoding="utf-8",
            )
    elif flavour == "cmd":
        result = host.run(
            ("certutil", "-hashfile", _hashable_path(path), algorithm.upper()),
            encoding="utf-8",
        )
    else:
        result = host.run(
            # `--` first: without it a file named `-z` or `-b` is read as an
            # OPTION, and `md5sum -z` hashes stdin -- which at EOF is the
            # empty-input digest, accepted by the parser and equal on both
            # sides, so two different files compared as identical.
            (f"{algorithm}sum", "--", _hashable_path(path)),
            encoding="utf-8",
        )
    return _parse_digest(result.stdout, algorithm)


def _hashable_path(path: object) -> str:
    """The path the BACKEND reads, which is what the remote shell must hash.

    A composite renders its logical text, and for SFTP that is not the same
    string: the backend prefixes `/`, so a relative sync root hashed
    `~/data/x` through the login shell while the transfer copied `/data/x`.
    Files whose home-directory twins happened to match were then reported in
    sync and never copied.
    """
    resolve = getattr(path, "_provider_path", None)
    provider = getattr(path, "provider", None)
    if callable(resolve) and provider is not None:
        try:
            backend = resolve(provider)
        except Exception:
            backend = path
    else:
        backend = path
    host_fspath = getattr(backend, "host_fspath", None)
    if callable(host_fspath):
        try:
            return str(host_fspath())
        except Exception:
            pass
    try:
        return os.fspath(backend)
    except (NotImplementedError, TypeError, ValueError):
        return str(backend)


def _parse_digest(output: typing.Union[bytes, str, None], algorithm: str) -> str:
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    expected = hashlib.new(algorithm).digest_size * 2
    for line in (output or "").splitlines():
        fields = line.split()
        if not fields:
            continue
        # Two shapes to accept: certutil spreads the digest across
        # space-separated byte pairs, so the whole line joins into one
        # candidate; md5sum emits "<digest>  <path>", so the first field is
        # the candidate. A path may itself look like a digest, hence the
        # first field rather than any field.
        for candidate in ("".join(fields), fields[0]):
            if len(candidate) == expected and _HEX_DIGEST.fullmatch(candidate):
                return candidate.casefold()
    raise ValueError(f"unable to parse {algorithm} checksum from host output")


def _stream_checksum(path: object, algorithm: str, chunk_size: int) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _is_local_owner(host: Host, path: object) -> bool:
    """Whether the owning host is THIS process's machine.

    Deliberately the host's scheme rather than the path's `is_local()`: an
    in-memory or otherwise process-resident backend also reports itself as
    local, and a composed host may reach a local path through a provider
    whose shell is the one the caller wants used. `local:` is the case where
    spawning a process to hash bytes we can already read is pure cost.
    """
    del path
    return getattr(host, "scheme", "") == "local"


def _host_path_token(host: Host) -> typing.Optional[tuple[object, ...]]:
    """Return the backend identity for a host without using path prefixes."""
    selector = getattr(host, "_path_selector", None)
    if selector is not None:
        return None
    try:
        sample = host.path()
    except (NotImplementedError, RuntimeError, OSError):
        return None
    return _path_token(sample)


def _host_owns_path(
    host: Host,
    path: object,
    owner_token: typing.Optional[tuple[object, ...]],
) -> bool:
    provider = getattr(path, "provider", None)
    selector = getattr(host, "_path_selector", None)
    if provider is not None and selector is not None:
        return any(
            provider is candidate for candidate in getattr(selector, "providers", ())
        )
    return owner_token is not None and _path_token(path) == owner_token


def _path_token(path: object) -> typing.Optional[tuple[object, ...]]:
    provider = getattr(path, "provider", None)
    if provider is not None:
        return ("provider", id(provider))

    backend_path = getattr(path, "_backend_path", None)
    if backend_path is not None and backend_path is not path:
        return _path_token(backend_path)

    backend = getattr(path, "backend", None)
    if backend is None:
        return ("local", type(path))

    container = getattr(backend, "container", None)
    if container is not None:
        return ("container", id(container))
    return ("backend", id(backend))
