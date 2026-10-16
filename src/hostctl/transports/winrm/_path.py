"""Windows paths over WinRM: the PowerShell-backed path backend."""

from __future__ import annotations

import base64
import errno
import io
import json
import ntpath
import stat as _stat
import subprocess
import typing
import uuid


from pathapi import FileStat


from ._pywinrm import _b64
from ...paths._backend import WindowsBackendPath


def _append_operation(encoded: str) -> str:
    """One chunk appended to the temporary file; also sizes the chunks."""
    return (
        "$b=[Convert]::FromBase64String('"
        + encoded
        + "');$s=[IO.File]::Open($p,[IO.FileMode]::Append,"
        "[IO.FileAccess]::Write,[IO.FileShare]::None);"
        "try{$s.Write($b,0,$b.Length)}finally{$s.Dispose()}"
    )


class WinRMPathBackend:
    """Structured PowerShell filesystem operations for :class:`WinRMPath`."""

    # `max_script_bytes` is the transport's per-call script budget; a write
    # batches as many chunks as fit in one script.
    chunk_size = 48 * 1024

    def __init__(
        self,
        run: typing.Callable[..., subprocess.CompletedProcess],
        *,
        max_script_bytes: int,
    ) -> None:
        self._run = run
        if max_script_bytes < 2_048:
            raise ValueError("max_script_bytes must be at least 2048")
        self.max_script_bytes = max_script_bytes

    def _script(
        self, path: str, body: str, *, target: typing.Optional[str] = None
    ) -> str:
        values = [
            # A headless native Invoke-Command runspace can have no valid
            # console handle. Keep the WinRS UTF-8 fix, but do not let
            # Console.OutputEncoding abort otherwise-valid filesystem work.
            "try{[Console]::OutputEncoding="
            "[Text.UTF8Encoding]::new($false)}catch{};"
            "$OutputEncoding=[Text.UTF8Encoding]::new($false)",
            "$ErrorActionPreference='Stop'",
            "$p=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{_b64(path)}'))",
        ]
        if target is not None:
            values.append(
                "$t=[Text.Encoding]::UTF8.GetString("
                f"[Convert]::FromBase64String('{_b64(target)}'))"
            )
        values.append(
            "try {" + body + "} catch {"
            "$e=$_.Exception;"
            # PowerShell wraps a .NET static-method failure in a
            # MethodInvocationException whose category is NotSpecified;
            # unwrapped, every `[IO.File]`/`[IO.Directory]` error is a bare
            # OSError, which staged_open's `except FileNotFoundError` misses.
            "while($e.InnerException -and ($e -is "
            "[System.Management.Automation.MethodInvocationException]))"
            "{$e=$e.InnerException};"
            "$c=[string]$_.CategoryInfo.Category;"
            "$k=if([IO.Directory]::Exists($p) -and "
            "($e -is [UnauthorizedAccessException])){'isdir'}"
            "elseif($c -eq 'ObjectNotFound'){'missing'}"
            "elseif($c -eq 'PermissionDenied' -or "
            "$e -is [UnauthorizedAccessException]){'permission'}"
            "elseif($c -eq 'ResourceExists'){'exists'}"
            "elseif($e -is [IO.FileNotFoundException] -or "
            "$e -is [IO.DirectoryNotFoundException] -or "
            "$_ -is [System.Management.Automation.ItemNotFoundException]){'missing'}"
            "elseif($e -is [IO.IOException] -and "
            "$e.HResult -eq -2147024816){'exists'}"
            # ERROR_ALREADY_EXISTS: a move onto an existing entry.
            "elseif($e -is [IO.IOException] -and "
            "$e.HResult -eq -2147024713){'exists'}"
            # ERROR_DIR_NOT_EMPTY: a delete of a non-empty directory.
            "elseif($e -is [IO.IOException] -and "
            "$e.HResult -eq -2147024751){'notempty'}"
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
            error_type, code = {
                "missing": (FileNotFoundError, errno.ENOENT),
                "permission": (PermissionError, errno.EACCES),
                "exists": (FileExistsError, errno.EEXIST),
                "isdir": (IsADirectoryError, errno.EISDIR),
                "notdir": (NotADirectoryError, errno.ENOTDIR),
                "notempty": (OSError, errno.ENOTEMPTY),
            }.get(kind, (OSError, errno.EIO))
            # The full tuple: an `OSError` with no `errno` and no `filename`
            # cannot be handled generically (`exc.errno == errno.ENOENT`) and
            # cannot name the path in a message -- a caller had only the
            # class and whatever text the remote happened to send.
            raise error_type(code, detail or "remote error", path)
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

    def stat(
        self, path: str, *, follow_symlinks: bool = True, _hops: int = 8
    ) -> FileStat:
        body = (
            self._metadata_script("Get-Item -LiteralPath $p -Force")
            + "|ConvertTo-Json -Compress"
        )
        value = json.loads(self._execute(path, body))
        result = self._stat_value(value)
        if follow_symlinks and _stat.S_ISLNK(result.st_mode):
            target = typing.cast(str, value.get("target") or "")
            if not target:
                raise OSError(errno.EINVAL, "cannot resolve reparse point", path)
            # A link's target is stored as written, and Windows resolves a
            # relative one against the LINK's directory. Re-stating it as an
            # absolute path looked it up from the drive root instead, so
            # `C:\app\current -> releases\v3` stat'd `releases\v3`.
            if not ntpath.isabs(target) and not target.startswith("\\\\"):
                target = ntpath.join(ntpath.dirname(path), target)
            if _hops <= 0:
                raise OSError(errno.ELOOP, "too many levels of symbolic links", path)
            try:
                return self.stat(target, follow_symlinks=True, _hops=_hops - 1)
            except OSError as exc:
                if getattr(exc, "errno", None) == errno.ELOOP:
                    raise
                # Keep the cause's errno, so a dangling link is a
                # FileNotFoundError, as local `stat` makes it.
                raise OSError(
                    exc.errno or errno.EIO, "cannot resolve reparse point", path
                ) from exc
        return result

    def scandir(self, path: str) -> typing.List[typing.Tuple[str, FileStat]]:
        # Follow the link, as `os.scandir` and `pathlib.Path.iterdir` do:
        # every reparse point is reported as S_IFLNK whatever it points at,
        # and a junction (a redirected profile folder, a DFS-style mount
        # point, "C:\Users\All Users") lists like the directory it is.
        current = self.stat(path, follow_symlinks=True)
        if not _stat.S_ISDIR(current.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "Not a directory", path)
        metadata = self._metadata_script("$_")
        body = (
            f"@((Get-ChildItem -LiteralPath $p -Force)|ForEach-Object{{{metadata}}})"
            "|ConvertTo-Json -Compress"
        )
        values = json.loads(self._execute(path, body) or "[]")
        if isinstance(values, dict):
            values = [values]
        return [(value["name"], self._stat_value(value)) for value in values]

    def read_bytes(self, path: str, offset: int = 0) -> bytes:
        """Read from `offset` to the end of the file in one script."""
        if offset < 0:
            raise ValueError("offset must be non-negative")
        seek = f"$s.Seek({offset},[IO.SeekOrigin]::Begin)|Out-Null;" if offset else ""
        body = (
            "$s=[IO.File]::OpenRead($p);try{" + seek + "$b=New-Object byte[] "
            f"{self.chunk_size};while(($n=$s.Read($b,0,$b.Length))-gt 0){{"
            "[Convert]::ToBase64String($b,0,$n)}}finally{$s.Dispose()}"
        )
        return b"".join(
            base64.b64decode(line)
            for line in self._execute(path, body).splitlines()
            if line
        )

    def read_range(self, path: str, offset: int, count: int) -> bytes:
        """Read one bounded byte range through PowerShell."""
        if offset < 0 or count < 0:
            raise ValueError("offset and count must be non-negative")
        body = (
            "$s=[IO.File]::OpenRead($p);try{$s.Seek(%d,[IO.SeekOrigin]::Begin)|Out-Null;"
            "$b=New-Object byte[] %d;$n=$s.Read($b,0,$b.Length);"
            "[Convert]::ToBase64String($b,0,$n)}finally{$s.Dispose()}" % (offset, count)
        )
        output = self._execute(path, body)
        return b"".join(base64.b64decode(line) for line in output.splitlines() if line)

    def open_read(self, path: str) -> io.BufferedReader:
        return io.BufferedReader(_WinRMReadStream(self, path), buffer_size=_READ_BUFFER)

    def exists(self, path: str, *, follow_symlinks: bool = True) -> bool:
        """Whether the path exists, from metadata alone."""
        try:
            self.stat(path, follow_symlinks=follow_symlinks)
        except FileNotFoundError:
            return False
        return True

    def write_bytes(self, path: str, value: bytes, *, exclusive: bool = False) -> None:
        temporary = path + ".hostctl-" + uuid.uuid4().hex
        try:
            self._execute(
                temporary,
                "$s=[IO.File]::Open($p,[IO.FileMode]::CreateNew,"
                "[IO.FileAccess]::Write,[IO.FileShare]::None);$s.Dispose()",
            )
            # One script per batch of chunks: a round trip per chunk would
            # cost far more than the base64 does.
            batches: typing.List[typing.List[str]] = []
            batch: typing.List[str] = []
            batch_size = 0
            # MEASURED, not guessed: the fixed cost is this path's own
            # prelude, which carries the base64 of the path itself.
            overhead = len(self._script(temporary, "", target=path).encode("utf-8"))
            room = self.max_script_bytes - overhead - len(_append_operation(""))
            payload_size = min(self.chunk_size, (room // 4) * 3)
            if payload_size < 3:
                raise OSError(
                    errno.ENAMETOOLONG,
                    f"the script budget ({self.max_script_bytes} bytes) cannot "
                    f"hold this path's prelude ({overhead} bytes) and one chunk",
                    path,
                )
            script_budget = self.max_script_bytes - overhead
            for offset in range(0, len(value), payload_size):
                encoded = base64.b64encode(
                    value[offset : offset + payload_size]
                ).decode("ascii")
                operation = _append_operation(encoded)
                if batch and batch_size + len(operation) + 1 > script_budget:
                    batches.append(batch)
                    batch = []
                    batch_size = 0
                batch.append(operation)
                batch_size += len(operation) + 1
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
                    # `[NullString]::Value`, not `$null`: PowerShell's .NET
                    # binder turns `$null` into "" for a `string` parameter,
                    # so `Replace` received an empty backup name and threw on
                    # every PowerShell version -- overwriting an existing file
                    # through a WinRM path could never succeed, and the old
                    # contents stayed put.
                    else "if([IO.File]::Exists($t)){"
                    "[IO.File]::Replace($p,$t,[NullString]::Value)}"
                    "else{[IO.File]::Move($p,$t)}"
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
            # NOT Remove-Item: on Windows PowerShell 5.1 -- the default remote
            # shell on every Windows Server -- a DIRECTORY reparse point whose
            # target has entries is treated as a directory with children, so
            # Remove-Item asks for confirmation. Every WinRM hop runs
            # powershell.exe with -NonInteractive, where the prompt cannot be
            # answered: it raised and left the junction in place, and only
            # when the target was non-empty, so the failure looked
            # intermittent. These .NET calls delete the link itself and
            # never recurse into what it points at.
            "if($i.PSIsContainer){[IO.Directory]::Delete($p,$false)}"
            "else{[IO.File]::Delete($p)}"
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

    def symlink(self, path: str, target: str) -> None:
        """Create ``path`` as a symbolic link to ``target``.

        Creating a symbolic link on Windows requires either an elevated
        session or Developer Mode; without one, ``New-Item`` fails with a
        privilege error which the shared error mapping surfaces as
        :class:`PermissionError`.  ``-ItemType SymbolicLink`` is used for
        both file and directory targets: Windows records the distinction
        from the target itself when it exists, and a dangling link is
        created as a file link (matching ``os.symlink`` defaults).
        """
        self._execute(
            path,
            "if([IO.Directory]::Exists($p) -or [IO.File]::Exists($p)){"
            "throw [IO.IOException]::new('Path exists',-2147024816)};"
            # A privilege failure raised by New-Item is a SecurityException or
            # a Win32 ERROR_PRIVILEGE_NOT_HELD (1314) IOException; normalize
            # both to the 'permission' marker the caller maps to
            # PermissionError, rather than letting them fall through to a
            # bare OSError.
            "try{New-Item -ItemType SymbolicLink -Path $p -Target $t "
            "-Force:$false|Out-Null}catch{"
            "$x=$_.Exception;"
            "if($x -is [Security.SecurityException] -or "
            "$x.HResult -eq -2147024891 -or $x.HResult -eq -2147023582 -or "
            "$x.Message -match 'privilege'){"
            "throw [UnauthorizedAccessException]::new($x.Message)};throw}",
            target=target,
        )

    def readlink(self, path: str) -> str:
        body = (
            "$i=Get-Item -LiteralPath $p -Force;"
            "if(-not ($i.Attributes -band [IO.FileAttributes]::ReparsePoint)){"
            "$m=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("
            "'not a symbolic link: '+$p));"
            "Write-Output ('HOSTCTL_ERROR:oserror:'+$m)}else{"
            "Write-Output ([Convert]::ToBase64String("
            "[Text.Encoding]::UTF8.GetBytes([string]$i.Target)))}"
        )
        output = self._execute(path, body).strip()
        if not output:
            raise OSError(errno.EINVAL, "cannot resolve reparse point", path)
        return base64.b64decode(output).decode("utf-8", "replace")

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


#: One range script per MiB for a partial read.
_READ_BUFFER = 1 << 20


class _WinRMReadStream(io.RawIOBase):
    """Lazy bounded reader backed by independent WinRM range requests."""

    def __init__(self, backend: WinRMPathBackend, path: str) -> None:
        self._backend = backend
        self._path = path
        self._offset = 0
        self._at_end = False

    def readable(self) -> bool:
        return True

    def readall(self) -> bytes:
        """The rest of the file in one script, which `read()` calls."""
        if self._at_end:
            return b""
        data = self._backend.read_bytes(self._path, self._offset)
        self._offset += len(data)
        self._at_end = True
        return data

    def readinto(self, target: bytearray) -> int:
        if not target or self._at_end:
            return 0
        data = self._backend.read_range(self._path, self._offset, len(target))
        if not data:
            return 0
        target[: len(data)] = data
        self._offset += len(data)
        return len(data)


class WinRMPath(WindowsBackendPath):
    """A Windows path whose I/O executes through a WinRM host."""

    __slots__ = ()
    _backend_type = WinRMPathBackend

    def _symlink_to(self, target, target_is_directory: bool = False):
        """Create this path as a symbolic link to ``target``.

        The PRIMITIVE, not the public method: `pathapi`'s wrapper adds
        `force=` (remove an existing entry first) and normalises a `str`
        target without re-parsing it as URI syntax.

        Windows only permits this from an elevated session or with
        Developer Mode enabled; otherwise the backend raises
        :class:`PermissionError`.  ``target_is_directory`` is accepted for
        :meth:`pathlib.Path.symlink_to` signature parity and ignored --
        ``New-Item -ItemType SymbolicLink`` infers the kind from the target.
        """
        self.backend.symlink(str(self), str(target))

    def readlink(self):
        return self.with_segments(self.backend.readlink(str(self)))

    def _mkdir(self, mode: int):
        self.backend.mkdir(str(self))

    def _chmod_on_backend(self, mode: int, follow_symlinks: bool) -> None:
        if not follow_symlinks:
            raise NotImplementedError("WinRMPath chmod does not support links")
        self.backend.chmod(str(self), mode)
