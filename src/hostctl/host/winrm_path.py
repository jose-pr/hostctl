"""Windows filesystem paths backed by PowerShell over WinRM."""

from __future__ import annotations

import base64
import io
import json
import stat as _stat
import subprocess
import typing
import uuid
import warnings

from pathlib import PurePath as _StdPurePath
from pathlib_next import Path, WindowsPathname
from pathlib_next.utils.stat import FileStat


def _encoded(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


class WinRMPathBackend:
    """Structured PowerShell filesystem operations for :class:`WinRMPath`."""

    # WinRS command lines are much smaller than a local PowerShell stdin
    # stream.  Keep each generated script below the conservative 6 KiB
    # budget used by pywinrm, while native adapters may opt into larger
    # batches through ``max_script_bytes``.
    chunk_size = 48 * 1024

    def __init__(
        self,
        run: typing.Callable[..., subprocess.CompletedProcess],
        *,
        max_script_bytes: int = 6_000,
    ) -> None:
        self._run = run
        if max_script_bytes < 2_048:
            raise ValueError("max_script_bytes must be at least 2048")
        self.max_script_bytes = max_script_bytes

    def _script(
        self, path: str, body: str, *, target: typing.Optional[str] = None
    ) -> str:
        values = [
            "$OutputEncoding=[Console]::OutputEncoding="
            "[Text.UTF8Encoding]::new($false)",
            "$ErrorActionPreference='Stop'",
            "$p=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{_encoded(path)}'))",
        ]
        if target is not None:
            values.append(
                "$t=[Text.Encoding]::UTF8.GetString("
                f"[Convert]::FromBase64String('{_encoded(target)}'))"
            )
        values.append(
            "try {" + body + "} catch {"
            "$e=$_.Exception;"
            "$c=[string]$_.CategoryInfo.Category;"
            "$k=if($c -eq 'ObjectNotFound'){'missing'}"
            "elseif($c -eq 'PermissionDenied' -or "
            "$e -is [UnauthorizedAccessException]){'permission'}"
            "elseif($c -eq 'ResourceExists'){'exists'}"
            "elseif($e -is [IO.FileNotFoundException] -or "
            "$e -is [IO.DirectoryNotFoundException] -or "
            "$_ -is [System.Management.Automation.ItemNotFoundException]){'missing'}"
            "elseif($e -is [IO.IOException] -and "
            "$e.HResult -eq -2147024816){'exists'}"
            "else{'oserror'};"
            "$m=[Convert]::ToBase64String("
            "[Text.Encoding]::UTF8.GetBytes($e.Message));"
            "Write-Output ('HOSTCTL_ERROR:'+$k+':'+$m)}"
        )
        return ";".join(values)

    def _execute(
        self, path: str, body: str, *, target: typing.Optional[str] = None
    ) -> str:
        result = self._run(
            self._script(path, body, target=target),
            check=False,
            encoding="utf-8",
        )
        output = result.stdout or ""
        marker = next(
            (line for line in output.splitlines() if line.startswith("HOSTCTL_ERROR:")),
            None,
        )
        if result.returncode or marker is not None:
            if marker is not None:
                _, kind, encoded = marker.split(":", 2)
                detail = base64.b64decode(encoded).decode("utf-8", "replace")
            else:
                message = result.stderr or ""
                if isinstance(message, bytes):
                    message = message.decode("utf-8", "replace")
                detail = message
                kind = "oserror"
            error_type = {
                "missing": FileNotFoundError,
                "permission": PermissionError,
                "exists": FileExistsError,
                "isdir": IsADirectoryError,
                "notdir": NotADirectoryError,
            }.get(kind, OSError)
            raise error_type(detail or path)
        return output

    @staticmethod
    def _metadata_script(expression: str) -> str:
        return (
            f"$i={expression};"
            "[pscustomobject]@{"
            "name=$i.Name;directory=[bool]$i.PSIsContainer;"
            "size=$(if($i.PSIsContainer){0}else{[long]$i.Length});"
            "mtime=([DateTimeOffset]$i.LastWriteTimeUtc).ToUnixTimeSeconds();"
            "readonly=[bool]($i.Attributes -band [IO.FileAttributes]::ReadOnly);"
            "link=[bool]($i.Attributes -band [IO.FileAttributes]::ReparsePoint);"
            "target=[string]$i.Target"
            "}"
        )

    @staticmethod
    def _stat_value(value: typing.Mapping[str, object]) -> FileStat:
        mode = _stat.S_IFDIR if value["directory"] else _stat.S_IFREG
        if value.get("link"):
            mode = _stat.S_IFLNK
        permissions = 0o444 if value.get("readonly") else 0o666
        if value["directory"]:
            permissions |= 0o111
        return FileStat(
            st_mode=mode | permissions,
            st_size=typing.cast(int, value["size"]),
            st_mtime=typing.cast(int, value["mtime"]),
        )

    def stat(self, path: str, *, follow_symlinks: bool = True) -> FileStat:
        body = (
            self._metadata_script("Get-Item -LiteralPath $p -Force")
            + "|ConvertTo-Json -Compress"
        )
        value = json.loads(self._execute(path, body))
        result = self._stat_value(value)
        if follow_symlinks and _stat.S_ISLNK(result.st_mode):
            target = typing.cast(str, value.get("target") or "")
            if not target:
                raise OSError(f"cannot resolve reparse point: {path}")
            try:
                return self.stat(target, follow_symlinks=True)
            except OSError as exc:
                raise OSError(f"cannot resolve reparse point: {path}") from exc
        return result

    def scandir(self, path: str) -> typing.List[typing.Tuple[str, FileStat]]:
        current = self.stat(path, follow_symlinks=False)
        if not _stat.S_ISDIR(current.st_mode):
            raise NotADirectoryError(path)
        metadata = self._metadata_script("$_")
        body = (
            f"@((Get-ChildItem -LiteralPath $p -Force)|ForEach-Object{{{metadata}}})"
            "|ConvertTo-Json -Compress"
        )
        values = json.loads(self._execute(path, body) or "[]")
        if isinstance(values, dict):
            values = [values]
        return [(value["name"], self._stat_value(value)) for value in values]

    def read_bytes(self, path: str) -> bytes:
        body = (
            "$s=[IO.File]::OpenRead($p);try{$b=New-Object byte[] "
            f"{self.chunk_size};while(($n=$s.Read($b,0,$b.Length))-gt 0){{"
            "[Convert]::ToBase64String($b,0,$n)}}finally{$s.Dispose()}"
        )
        return b"".join(
            base64.b64decode(line)
            for line in self._execute(path, body).splitlines()
            if line
        )

    def write_bytes(self, path: str, value: bytes, *, exclusive: bool = False) -> None:
        temporary = path + ".hostctl-" + uuid.uuid4().hex
        try:
            self._execute(
                temporary,
                "$s=[IO.File]::Open($p,[IO.FileMode]::CreateNew,"
                "[IO.FileAccess]::Write,[IO.FileShare]::None);$s.Dispose()",
            )
            # Build one script per transport-sized batch.  This avoids a
            # round-trip for every tiny chunk while never exceeding the
            # WinRS command-line budget.
            batches: typing.List[typing.List[str]] = []
            batch: typing.List[str] = []
            batch_size = 0
            payload_size = min(self.chunk_size, 1536)
            script_budget = max(1024, self.max_script_bytes - 1_200)
            for offset in range(0, len(value), payload_size):
                encoded = base64.b64encode(
                    value[offset : offset + payload_size]
                ).decode("ascii")
                operation = (
                    "$b=[Convert]::FromBase64String('"
                    + encoded
                    + "');$s=[IO.File]::Open($p,[IO.FileMode]::Append,"
                    "[IO.FileAccess]::Write,[IO.FileShare]::None);"
                    "try{$s.Write($b,0,$b.Length)}finally{$s.Dispose()}"
                )
                if batch and batch_size + len(operation) > script_budget:
                    batches.append(batch)
                    batch = []
                    batch_size = 0
                batch.append(operation)
                batch_size += len(operation)
            if batch:
                batches.append(batch)
            for operations in batches:
                self._execute(temporary, ";".join(operations))
            self._execute(
                temporary,
                (
                    "if([IO.File]::Exists($t)){"
                    "throw [IO.IOException]::new('Path exists',-2147024816)};"
                    "[IO.File]::Move($p,$t)"
                    if exclusive
                    else "if([IO.File]::Exists($t)){"
                    "[IO.File]::Replace($p,$t,$null)}else{[IO.File]::Move($p,$t)}"
                ),
                target=path,
            )
        except Exception:
            self.unlink(temporary, missing_ok=True)
            raise

    def mkdir(self, path: str) -> None:
        self._execute(
            path,
            "if([IO.Directory]::Exists($p) -or [IO.File]::Exists($p)){"
            "throw [IO.IOException]::new('Path exists',-2147024816)};"
            "$parent=[IO.Path]::GetDirectoryName($p);"
            "if($parent -and -not [IO.Directory]::Exists($parent)){"
            "throw [IO.DirectoryNotFoundException]::new($parent)};"
            "[IO.Directory]::CreateDirectory($p)|Out-Null",
        )

    def unlink(self, path: str, *, missing_ok: bool = False) -> None:
        body = (
            "$i=Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue;"
            "if($null -eq $i){"
            + ("" if missing_ok else "throw [IO.FileNotFoundException]::new($p)")
            + "}elseif($i.Attributes -band [IO.FileAttributes]::ReparsePoint){"
            "Remove-Item -LiteralPath $p -Force"
            "}elseif($i.PSIsContainer){"
            "$m=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($p));"
            "Write-Output ('HOSTCTL_ERROR:isdir:'+$m)}else{[IO.File]::Delete($p)}"
        )
        self._execute(path, body)

    def rmdir(self, path: str) -> None:
        self._execute(
            path,
            "$i=Get-Item -LiteralPath $p -Force;"
            "if(-not $i.PSIsContainer){"
            "$m=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($p));"
            "Write-Output ('HOSTCTL_ERROR:notdir:'+$m)}else{"
            "[IO.Directory]::Delete($p,$false)}",
        )

    def rename(self, path: str, target: str) -> None:
        self._execute(
            path,
            "if([IO.Directory]::Exists($p)){[IO.Directory]::Move($p,$t)}"
            "else{[IO.File]::Move($p,$t)}",
            target=target,
        )

    def chmod(self, path: str, mode: int) -> None:
        readonly = "$false" if mode & 0o200 else "$true"
        self._execute(
            path,
            "$i=Get-Item -LiteralPath $p -Force;"
            f"$i.Attributes=if({readonly}){{[IO.FileAttributes]($i.Attributes -bor "
            "[IO.FileAttributes]::ReadOnly)}else{[IO.FileAttributes]($i.Attributes "
            "-band (-bnot [IO.FileAttributes]::ReadOnly))};"
            "Set-ItemProperty -LiteralPath $p -Name Attributes -Value $i.Attributes",
        )


class _WriteBackBytesIO(io.BytesIO):
    def __init__(
        self,
        value: bytes,
        commit: typing.Optional[typing.Callable[[bytes], None]],
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

    def __del__(self):
        if getattr(self, "_commit", None) is not None and not self.closed:
            warnings.warn(
                "unclosed WinRM write stream discarded without committing",
                ResourceWarning,
                stacklevel=2,
            )
            self._commit = None
        try:
            super().close()
        except Exception:
            pass


class WinRMPath(WindowsPathname, Path):
    """A Windows path whose I/O executes through a WinRM host."""

    __slots__ = ("_backend",)

    def __init__(self, *segments, backend=None):
        # Python 3.14's pathlib.PurePath.__init__ no longer accepts kwargs.
        # Path state is initialized by __new__; backend is attached there.
        if not hasattr(self, "_raw_paths") and not hasattr(self, "_parts"):
            _StdPurePath.__init__(self, *segments)

    def __new__(
        cls,
        *segments: typing.Union[str, WindowsPathname],
        backend: typing.Optional[WinRMPathBackend] = None,
    ):
        inherited = next(
            (segment.backend for segment in segments if isinstance(segment, WinRMPath)),
            None,
        )
        self = super().__new__(cls, *segments)
        self._backend = backend or inherited
        if self._backend is None:
            raise TypeError("WinRMPath requires a backend")
        return self

    @property
    def backend(self) -> WinRMPathBackend:
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
            or len(mode.replace("t", "")) != 1 + mode.count("+")
            or mode.count("t") > 1
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
        self.backend.mkdir(str(self))

    def chmod(self, mode: int, *, follow_symlinks: bool = True):
        if not follow_symlinks:
            raise NotImplementedError("WinRMPath chmod does not support links")
        self.backend.chmod(str(self), mode)

    def unlink(self, missing_ok=False):
        self.backend.unlink(str(self), missing_ok=missing_ok)

    def rmdir(self):
        self.backend.rmdir(str(self))

    def rename(self, target):
        if not isinstance(target, WinRMPath):
            target = type(self)(target, backend=self.backend)
        if target.backend is not self.backend:
            raise ValueError("cannot rename across WinRM path backends")
        self.backend.rename(str(self), str(target))
        return target
