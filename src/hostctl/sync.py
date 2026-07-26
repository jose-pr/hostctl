"""Checksum and progress helpers for :mod:`pathlib_next` copy and sync."""

from __future__ import annotations

import hashlib
import os
import re
import typing

from pathlib_next.utils.sync import PathAndStat

if typing.TYPE_CHECKING:
    from .host import Host


_REMOTE_ALGORITHMS = frozenset(("md5", "sha1", "sha256", "sha384", "sha512"))
_HEX_DIGEST = re.compile(r"^[0-9a-fA-F]+$")


def stat_checksum(entry: PathAndStat) -> tuple[int, float]:
    """Return the cached size and modification time without reading content."""
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
                return _remote_checksum(host, entry.path, normalized)
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

    def read(self, size: int = -1) -> bytes:
        data = self.reader.read(size)
        self.bytes_read += len(data)
        self.callback(self.bytes_read, self.total)
        return data

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
            ("certutil", "-hashfile", os.fspath(path), algorithm.upper()),
            encoding="utf-8",
        )
    else:
        result = host.run(
            (f"{algorithm}sum", os.fspath(path)),
            encoding="utf-8",
        )
    return _parse_digest(result.stdout, algorithm)


def _parse_digest(output: typing.Union[bytes, str, None], algorithm: str) -> str:
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    expected = hashlib.new(algorithm).digest_size * 2
    for line in (output or "").splitlines():
        compact = "".join(line.split())
        candidates = (compact, line.strip().split(maxsplit=1)[0])
        for candidate in candidates:
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
