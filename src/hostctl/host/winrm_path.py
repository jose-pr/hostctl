"""Windows filesystem paths backed by PowerShell over WinRM."""

from __future__ import annotations

import base64
import io
import json
import stat as _stat
import subprocess
import typing
import uuid

from pathlib import PurePath as _StdPurePath
from pathlib_next import Path, WindowsPathname
from pathlib_next.utils.stat import FileStat


def _encoded(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


class WinRMPathBackend:
    """Structured PowerShell filesystem operations for :class:`WinRMPath`."""

    chunk_size = 48 * 1024

    def __init__(self, run: typing.Callable[..., subprocess.CompletedProcess]) -> None:
        self._run = run

    def _script(
        self, path: str, body: str, *, target: typing.Optional[str] = None
    ) -> str:
        values = [
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
                kind, _, detail = message.partition("\n")
            error_type = {
                "missing": FileNotFoundError,
                "permission": PermissionError,
                "exists": FileExistsError,
                "isdir": IsADirectoryError,
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
            "link=[bool]($i.Attributes -band [IO.FileAttributes]::ReparsePoint)"
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
            raise NotImplementedError(
                "WinRMPath cannot portably follow Windows reparse points"
            )
        return result

    def scandir(self, path: str) -> typing.List[typing.Tuple[str, FileStat]]:
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
            "if($n -eq $b.Length){$c=$b}else{$c=$b[0..($n-1)]};"
            "[Convert]::ToBase64String($c)}}finally{$s.Dispose()}"
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
            for offset in range(0, len(value), self.chunk_size):
                encoded = base64.b64encode(
                    value[offset : offset + self.chunk_size]
                ).decode("ascii")
                self._execute(
                    temporary,
                    "$b=[Convert]::FromBase64String('"
                    + encoded
                    + "');$s=[IO.File]::Open($p,[IO.FileMode]::Append,"
                    "[IO.FileAccess]::Write,[IO.FileShare]::None);"
                    "try{$s.Write($b,0,$b.Length)}finally{$s.Dispose()}",
                )
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
        missing = (
            "" if missing_ok else ("else{throw [IO.FileNotFoundException]::new($p)}")
        )
        body = (
            "if([IO.Directory]::Exists($p)){"
            "$m=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($p));"
            "Write-Output ('HOSTCTL_ERROR:isdir:'+$m)}"
            "elseif([IO.File]::Exists($p)){[IO.File]::Delete($p)}" + missing
        )
        self._execute(path, body)

    def rmdir(self, path: str) -> None:
        self._execute(path, "[IO.Directory]::Delete($p,$false)")

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
            "$i=Get-Item -LiteralPath $p -Force;" f"$i.IsReadOnly={readonly}",
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
