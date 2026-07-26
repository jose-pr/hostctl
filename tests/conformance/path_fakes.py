"""Protocol-shaped filesystem fakes for the path conformance battery.

These adapters exercise the production path/backend classes without opening
network connections or evaluating a generated PowerShell script.  Each fake
translates only the wire operations emitted by its production backend onto an
isolated temporary filesystem.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import Dict, Iterable, Optional, Tuple

from pathlib_next.uri.schemes.sftp import BaseSftpBackend
from pathlib_next.utils.stat import FileStat


class _Sandbox:
    """Map a target-flavoured absolute path into a temporary local root."""

    def __init__(self, flavour: str) -> None:
        self.flavour = flavour
        self._temporary = tempfile.TemporaryDirectory(prefix="hostctl-path-fake-")
        self.root = Path(self._temporary.name)
        # A real server stores the caller's target string verbatim and
        # resolves it in its own namespace.  The sandbox's remote->local
        # mapping is lossy, so the raw target is recorded alongside the link
        # rather than recovered by reversing the mapping.
        self._link_targets: Dict[str, str] = {}

    def close(self) -> None:
        self._temporary.cleanup()

    def symlink(self, remote: str, remote_target: str) -> None:
        """Create a link at ``remote`` storing ``remote_target`` verbatim."""
        link = self.local(remote)
        if link.exists() or link.is_symlink():
            raise FileExistsError(remote)
        # Point the on-disk link at the sandbox-mapped target so following it
        # behaves like the real server would; a target that does not exist
        # stays dangling, exactly as on the wire.
        link.symlink_to(self.local(remote_target))
        self._link_targets[str(link)] = remote_target

    def readlink(self, remote: str) -> str:
        link = self.local(remote)
        if not link.is_symlink():
            raise OSError(f"not a symbolic link: {remote}")
        return self.stored_target(link)

    def stored_target(self, link: Path) -> str:
        """Return the verbatim target recorded for an already-mapped link."""
        return self._link_targets.get(str(link), os.readlink(link))

    def local(self, remote: str) -> Path:
        if self.flavour == "windows":
            value = PureWindowsPath(remote)
            parts = list(value.parts)
            if value.drive:
                drive = value.drive.rstrip(":").replace("\\", "_")
                parts = parts[1:]
                components = [drive or "drive", *parts]
            else:
                components = parts
        else:
            value = PurePosixPath(remote)
            components = list(value.parts)
            if components and components[0] == "/":
                components = components[1:]
        cleaned = [
            part.replace("/", "_").replace("\\", "_").replace(":", "_")
            for part in components
            if part not in ("", ".", "/", "\\")
        ]
        result = self.root.joinpath(*cleaned)
        try:
            result.relative_to(self.root)
        except ValueError as exc:  # pragma: no cover - defensive invariant
            raise ValueError("fake path escaped its sandbox") from exc
        return result

    @staticmethod
    def metadata(path: Path, *, follow_symlinks: bool = True) -> FileStat:
        value = path.stat() if follow_symlinks else path.lstat()
        return FileStat.from_stat(value)


class LocalSftpClient:
    """Paramiko-shaped client consumed by pathlib_next's real SftpPath."""

    def __init__(self) -> None:
        self.sandbox = _Sandbox("posix")

    def close(self) -> None:
        self.sandbox.close()

    def _path(self, remote: str) -> Path:
        return self.sandbox.local(remote)

    def stat(self, path: str):
        return self._path(path).stat()

    def lstat(self, path: str):
        return self._path(path).lstat()

    def listdir_attr(self, path: str):
        result = []
        for child in sorted(self._path(path).iterdir(), key=lambda item: item.name):
            value = child.lstat()
            result.append(
                SimpleNamespace(
                    filename=child.name,
                    st_mode=value.st_mode,
                    st_ino=value.st_ino,
                    st_dev=value.st_dev,
                    st_nlink=value.st_nlink,
                    st_uid=value.st_uid,
                    st_gid=value.st_gid,
                    st_size=value.st_size,
                    st_atime=value.st_atime,
                    st_mtime=value.st_mtime,
                    st_ctime=value.st_ctime,
                )
            )
        return result

    def open(self, path: str, mode: str, buffering: int = -1):
        # SFTP clients expose byte streams for their historical ``r``/``w``
        # modes; pathlib_next supplies text decoding above this layer.
        binary_mode = mode if "b" in mode else mode + "b"
        return self._path(path).open(binary_mode, buffering=buffering)

    def mkdir(self, path: str, mode: int):
        self._path(path).mkdir(mode=mode)

    def chmod(self, path: str, mode: int, **_options):
        self._path(path).chmod(mode)

    def remove(self, path: str):
        self._path(path).unlink()

    def rmdir(self, path: str):
        self._path(path).rmdir()

    def rename(self, path: str, target: str):
        source = self._path(path)
        destination = self._path(target)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(target)
        source.rename(destination)

    def symlink(self, target: str, path: str):
        # SFTP's wire order is (target, path); the sandbox takes (path, target).
        self.sandbox.symlink(path, target)

    def readlink(self, path: str):
        return self.sandbox.readlink(path)


class LocalSftpBackend(BaseSftpBackend):
    """Backend instance which keeps one cached fake SFTP client."""

    def __init__(self) -> None:
        self._client = LocalSftpClient()

    def client(self, _source):
        return self._client

    def close(self) -> None:
        self._client.close()

    def invalidate(self, _source) -> None:
        # The production SSH transport invalidates sources during close.
        # This fake has no connection cache beyond its one owned client.
        return None


class WinRMFilesystemRunner:
    """Interpret WinRMPathBackend's structured scripts without executing them."""

    _PATH = re.compile(r"\$p=.*?FromBase64String\('([^']+)'\)")
    _TARGET = re.compile(r"\$t=.*?FromBase64String\('([^']+)'\)")
    _PAYLOAD = re.compile(r"\$b=\[Convert\]::FromBase64String\('([^']+)'\)")
    _SEEK = re.compile(r"\$s\.Seek\((\d+),")
    _BUFFER = re.compile(r"New-Object byte\[\] (\d+)")

    def __init__(self) -> None:
        self.sandbox = _Sandbox("windows")
        self.sandbox.local("C:\\").mkdir()

    def close(self) -> None:
        self.sandbox.close()

    @classmethod
    def _paths(cls, script: str) -> Tuple[str, Optional[str]]:
        path_match = cls._PATH.search(script)
        if path_match is None:
            raise AssertionError("WinRM script omitted its encoded path")
        target_match = cls._TARGET.search(script)
        path = base64.b64decode(path_match.group(1)).decode("utf-8", "strict")
        target = (
            base64.b64decode(target_match.group(1)).decode("utf-8", "strict")
            if target_match is not None
            else None
        )
        return path, target

    @staticmethod
    def _error(script: str, kind: str, detail: str):
        encoded = base64.b64encode(detail.encode("utf-8")).decode("ascii")
        return subprocess.CompletedProcess(
            script, 0, "HOSTCTL_ERROR:%s:%s" % (kind, encoded), ""
        )

    def _json_metadata(self, path: Path) -> Dict[str, object]:
        value = path.lstat()
        target = ""
        if path.is_symlink():
            # Report the target the caller stored, not the sandbox-mapped
            # one: WinRMPathBackend.stat() follows by re-issuing stat() on
            # this string, which is mapped again on the way back in.
            target = self.sandbox.stored_target(path)
        return {
            "name": path.name,
            "directory": path.is_dir(),
            "size": 0 if path.is_dir() else value.st_size,
            "mtime": int(value.st_mtime),
            "readonly": not bool(value.st_mode & stat.S_IWUSR),
            "link": path.is_symlink(),
            "target": target,
        }

    def __call__(self, script: str, **_options):
        remote, remote_target = self._paths(script)
        path = self.sandbox.local(remote)
        target = (
            self.sandbox.local(remote_target) if remote_target is not None else None
        )
        try:
            output = self._dispatch(script, path, target, remote, remote_target)
        except FileNotFoundError:
            return self._error(script, "missing", remote)
        except FileExistsError:
            return self._error(script, "exists", remote)
        except IsADirectoryError:
            return self._error(script, "isdir", remote)
        except NotADirectoryError:
            return self._error(script, "notdir", remote)
        except PermissionError:
            return self._error(script, "permission", remote)
        return subprocess.CompletedProcess(script, 0, output, "")

    def _dispatch(
        self,
        script: str,
        path: Path,
        target: Optional[Path],
        remote: str,
        remote_target: Optional[str],
    ) -> str:
        if "Get-ChildItem -LiteralPath $p" in script:
            if not path.is_dir():
                raise NotADirectoryError(str(path))
            values = [
                self._json_metadata(child)
                for child in sorted(path.iterdir(), key=lambda item: item.name)
            ]
            return json.dumps(values, separators=(",", ":"))

        if "Get-Item -LiteralPath $p" in script and "ConvertTo-Json" in script:
            return json.dumps(self._json_metadata(path), separators=(",", ":"))

        if "[IO.File]::OpenRead($p)" in script:
            data = path.read_bytes()
            seek = self._SEEK.search(script)
            if seek is not None:
                count = self._BUFFER.search(script)
                assert count is not None
                offset = int(seek.group(1))
                data = data[offset : offset + int(count.group(1))]
                return base64.b64encode(data).decode("ascii")
            chunk = 48 * 1024
            return "\n".join(
                base64.b64encode(data[offset : offset + chunk]).decode("ascii")
                for offset in range(0, len(data), chunk)
            )

        if "[IO.FileMode]::CreateNew" in script:
            with path.open("xb"):
                pass
            return ""

        payloads = self._PAYLOAD.findall(script)
        if payloads:
            with path.open("ab") as stream:
                for payload in payloads:
                    stream.write(base64.b64decode(payload))
            return ""

        if target is not None and (
            "[IO.File]::Move($p,$t)" in script
            or "[IO.Directory]::Move($p,$t)" in script
            or "[IO.File]::Replace($p,$t,$null)" in script
        ):
            exclusive = "throw [IO.IOException]::new('Path exists'" in script
            if target.exists() and exclusive:
                raise FileExistsError(str(target))
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            path.rename(target)
            return ""

        if "New-Item -ItemType SymbolicLink" in script:
            assert remote_target is not None, "symlink script omitted its target"
            try:
                self.sandbox.symlink(remote, remote_target)
            except OSError as exc:
                # Windows raises WinError 1314 (ERROR_PRIVILEGE_NOT_HELD)
                # without elevation or Developer Mode.  The production script
                # maps that to the 'permission' marker, so the fake must
                # produce the same PermissionError rather than a bare OSError.
                if getattr(exc, "winerror", None) == 1314:
                    raise PermissionError(remote) from exc
                raise
            return ""

        if "not a symbolic link: " in script:
            if not path.is_symlink():
                # The production script emits this marker itself rather than
                # throwing, so the fake reproduces its stdout, not an
                # exception.
                detail = "not a symbolic link: " + str(path)
                encoded = base64.b64encode(detail.encode("utf-8")).decode("ascii")
                return "HOSTCTL_ERROR:oserror:" + encoded
            value = self.sandbox.readlink(remote)
            return base64.b64encode(value.encode("utf-8")).decode("ascii")

        if "[IO.Directory]::CreateDirectory($p)" in script:
            path.mkdir()
            return ""

        if "[IO.Directory]::Delete($p,$false)" in script:
            path.rmdir()
            return ""

        if "[IO.File]::Delete($p)" in script:
            if not path.exists():
                if "throw [IO.FileNotFoundException]" in script:
                    raise FileNotFoundError(str(path))
                return ""
            if path.is_dir():
                raise IsADirectoryError(str(path))
            path.unlink()
            return ""

        if "Set-ItemProperty -LiteralPath $p -Name Attributes" in script:
            current = path.stat().st_mode
            writable = "$false" in script
            path.chmod(current | stat.S_IWUSR if writable else current & ~stat.S_IWUSR)
            return ""

        raise AssertionError("unrecognized WinRM filesystem script")


QGA_FILE_COMMANDS = frozenset(
    (
        "guest-file-open",
        "guest-file-read",
        "guest-file-write",
        "guest-file-seek",
        "guest-file-flush",
        "guest-file-close",
    )
)


class LocalQgaTransport:
    """QGA wire adapter with real handles over an isolated guest filesystem."""

    def __init__(self) -> None:
        self.sandbox = _Sandbox("posix")
        self._handles: Dict[int, object] = {}
        self._next_handle = 1
        self._exec_result = None

    def close(self) -> None:
        for stream in tuple(self._handles.values()):
            stream.close()
        self._handles.clear()
        self.sandbox.close()

    def execute(self, request, timeout=None):
        command = request["execute"]
        arguments = request.get("arguments", {})
        if command == "guest-ping":
            return {}
        if command == "guest-info":
            commands = QGA_FILE_COMMANDS | {
                "guest-exec",
                "guest-exec-status",
                "guest-get-osinfo",
                "guest-get-host-name",
            }
            return {
                "supported_commands": [
                    {"name": name, "enabled": True} for name in sorted(commands)
                ]
            }
        if command == "guest-get-osinfo":
            return {
                "id": "linux",
                "pretty-name": "Linux",
                "version": "fake",
                "machine": "x86_64",
            }
        if command == "guest-get-host-name":
            return {"host-name": "fake-qemu"}
        if command == "guest-exec":
            environment = os.environ.copy()
            for item in arguments.get("env", ()):
                key, value = item.split("=", 1)
                environment[key] = value
            input_data = arguments.get("input-data")
            payload = base64.b64decode(input_data) if input_data is not None else None
            self._exec_result = subprocess.run(
                [arguments["path"], *arguments.get("arg", ())],
                capture_output=True,
                check=False,
                env=environment,
                input=payload,
                timeout=timeout,
            )
            return {"pid": 1}
        if command == "guest-exec-status":
            result = self._exec_result
            if result is None:
                raise AssertionError("guest-exec-status preceded guest-exec")
            return {
                "exited": True,
                "exitcode": result.returncode,
                "out-data": base64.b64encode(result.stdout or b"").decode("ascii"),
                "err-data": base64.b64encode(result.stderr or b"").decode("ascii"),
            }
        if command == "guest-file-open":
            path = self.sandbox.local(arguments["path"])
            stream = path.open(arguments["mode"])
            handle = self._next_handle
            self._next_handle += 1
            self._handles[handle] = stream
            return handle
        handle = arguments.get("handle")
        stream = self._handles[handle]
        if command == "guest-file-read":
            data = stream.read(arguments["count"])
            return {
                "count": len(data),
                "buf-b64": base64.b64encode(data).decode("ascii"),
                "eof": len(data) < arguments["count"],
            }
        if command == "guest-file-write":
            data = base64.b64decode(arguments["buf-b64"])
            return {"count": stream.write(data)}
        if command == "guest-file-seek":
            whence = arguments["whence"]
            if isinstance(whence, dict):
                whence = {"set": 0, "cur": 1, "end": 2}[whence["name"]]
            position = stream.seek(arguments["offset"], whence)
            return {"position": position, "eof": False}
        if command == "guest-file-flush":
            stream.flush()
            return {}
        if command == "guest-file-close":
            stream.close()
            del self._handles[handle]
            return {}
        raise AssertionError(command)


class LocalQgaPathHelper:
    """Structured metadata/mutation helper used by QgaPathBackend."""

    def __init__(self, transport: LocalQgaTransport) -> None:
        self.transport = transport

    def _path(self, remote: str) -> Path:
        return self.transport.sandbox.local(remote)

    def stat(self, path: str, *, follow_symlinks: bool = True):
        return self.transport.sandbox.metadata(
            self._path(path), follow_symlinks=follow_symlinks
        )

    def scandir(self, path: str) -> Iterable[Tuple[str, FileStat]]:
        directory = self._path(path)
        if not directory.is_dir():
            raise NotADirectoryError(path)
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            yield child.name, self.transport.sandbox.metadata(
                child, follow_symlinks=False
            )

    def mkdir(self, path: str, mode: int) -> None:
        self._path(path).mkdir(mode=mode)

    def unlink(self, path: str, *, missing_ok: bool = False) -> None:
        self._path(path).unlink(missing_ok=missing_ok)

    def rmdir(self, path: str) -> None:
        self._path(path).rmdir()

    def rename(self, path: str, target: str, *, replace: bool = False) -> None:
        source = self._path(path)
        destination = self._path(target)
        if destination.exists() and not replace:
            raise FileExistsError(target)
        if replace:
            source.replace(destination)
        else:
            source.rename(destination)

    def chmod(self, path: str, mode: int, *, follow_symlinks: bool = True) -> None:
        if not follow_symlinks:
            raise NotImplementedError("fake QGA helper cannot chmod a link itself")
        self._path(path).chmod(mode)
