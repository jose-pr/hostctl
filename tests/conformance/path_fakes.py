"""Protocol-shaped filesystem fakes for the path conformance battery.

These adapters exercise the production path/backend classes without opening
network connections or evaluating a generated PowerShell script.  Each fake
translates only the wire operations emitted by its production backend onto an
isolated temporary filesystem.
"""

from __future__ import annotations

import base64
import errno
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import Dict, Iterable, Optional, Tuple

from sftppath import BaseSFTPBackend
from pathapi import FileStat


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
        # Idempotent: a host closes its transport and the battery closes the
        # fake, so this runs twice, and `TemporaryDirectory.cleanup()` on an
        # already-removed directory raises.
        temporary, self._temporary = self._temporary, None
        if temporary is not None:
            temporary.cleanup()

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
    """Paramiko-shaped client consumed by sftppath's real SFTPPath."""

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
        # modes; pathapi supplies text decoding above this layer.
        binary_mode = mode if "b" in mode else mode + "b"
        return self._path(path).open(binary_mode, buffering=buffering)

    def mkdir(self, path: str, mode: int):
        self._path(path).mkdir(mode=mode)

    def chmod(self, path: str, mode: int, **_options):
        self._path(path).chmod(mode)

    def utime(self, path: str, times):
        os.utime(self._path(path), times)

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


class LocalSftpBackend(BaseSFTPBackend):
    """Backend instance which keeps one cached fake SFTP client."""

    def __init__(self) -> None:
        self._client = LocalSftpClient()

    def client(self):
        return self._client

    def close(self) -> None:
        self._client.close()


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
            ranged = "while((" not in script
            if seek is not None and ranged:
                count = self._BUFFER.search(script)
                assert count is not None
                offset = int(seek.group(1))
                data = data[offset : offset + int(count.group(1))]
                return base64.b64encode(data).decode("ascii")
            if seek is not None:
                data = data[int(seek.group(1)) :]
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

        # Before the rmdir script: unlink's script also names
        # `[IO.Directory]::Delete`, for a directory reparse point.
        if "[IO.File]::Delete($p)" in script:
            if not path.exists():
                if "throw [IO.FileNotFoundException]" in script:
                    raise FileNotFoundError(str(path))
                return ""
            if path.is_dir():
                raise IsADirectoryError(str(path))
            path.unlink()
            return ""

        if "[IO.Directory]::Delete($p,$false)" in script:
            path.rmdir()
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

    def __init__(
        self,
        flavour: str = "posix",
        *,
        agent_version: Optional[str] = "8.2.2",
    ) -> None:
        self.flavour = flavour
        self.agent_version = agent_version
        if flavour == "windows":
            # The file RPCs and the PowerShell scripts must see one guest
            # filesystem, so the transport borrows the runner's sandbox.
            self._runner: Optional[WinRMFilesystemRunner] = WinRMFilesystemRunner()
            self.sandbox = self._runner.sandbox
            self._programs = None
        else:
            self._runner = None
            self.sandbox = _Sandbox("posix")
            self._programs = GnuGuestPrograms(self.sandbox)
        self._handles: Dict[int, object] = {}
        self._next_handle = 1
        self._exec_process = None
        self._exec_output = None
        self._exec_result = None
        self._exec_pid = 0
        #: Every `(program, argv)` a guest-exec asked for, in order.
        self.exec_calls: list = []

    def _end_exec(self) -> None:
        """End the current guest process and release its pipes.

        Called when a new `guest-exec` REPLACES this one as well as at
        close: the fake keeps a single process, so starting a second
        command used to drop the first `Popen` unreaped and its two pipes
        unclosed. Windows GC timing hid that; on Linux it surfaced as
        `ResourceWarning: subprocess N is still running` -- attributed,
        through warnings-as-errors, to whatever test happened to run next.
        """
        process, self._exec_process = self._exec_process, None
        self._exec_output = None
        self._exec_result = None
        if process is None:
            return
        # Order matters. `communicate()` READS the pipes, and a
        # shell-rendered command's grandchild still holds them open after
        # the shell itself is killed -- so it blocked, timed out, and left
        # the `Popen` unreaped, which is the `ResourceWarning: subprocess N
        # is still running` this exists to prevent. Close first, then wait.
        for pipe in (process.stdin, process.stdout, process.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass
        if process.poll() is None:
            # A timed-out guest command is ORPHANED by contract, so the
            # fake is what ends it.
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except Exception:
            pass

    def _emulate(self, arguments):
        """Answer a guest-exec this fake interprets, or `None` to run it."""
        program = arguments["path"]
        argv = tuple(arguments.get("arg", ()))
        self.exec_calls.append((program, argv))
        if self._runner is not None:
            if program != "powershell.exe":
                return None
            if argv != ("-NoProfile", "-NonInteractive", "-Command", "-"):
                raise AssertionError("unexpected powershell argv: %r" % (argv,))
            script = base64.b64decode(arguments["input-data"]).decode("utf-8")
            if script.strip() == "exit 0":
                return 0, b"", b""
            result = self._runner(script)
            stdout = result.stdout or ""
            if isinstance(stdout, str):
                stdout = stdout.encode("utf-8")
            return result.returncode, stdout, b""
        return self._programs(program, argv)

    def close(self) -> None:
        for stream in tuple(self._handles.values()):
            stream.close()
        self._handles.clear()
        self._end_exec()
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
            reply = {
                "supported_commands": [
                    {"name": name, "enabled": True} for name in sorted(commands)
                ]
            }
            if self.agent_version is not None:
                reply["version"] = self.agent_version
            return reply
        if command == "guest-get-osinfo":
            if self.flavour == "windows":
                return {
                    "id": "mswindows",
                    "name": "Microsoft Windows",
                    "kernel-name": "Windows NT",
                    "machine": "x86_64",
                }
            return {
                "id": "linux",
                "pretty-name": "Linux",
                "kernel-name": "Linux",
                "version": "fake",
                "machine": "x86_64",
            }
        if command == "guest-get-host-name":
            return {"host-name": "fake-qemu"}
        if command == "guest-exec":
            # Start it and RETURN, as qemu-ga does. Running the child
            # synchronously here and reporting `exited: True` from the first
            # status made the executor's poll loop unreachable through this
            # fake -- the backoff, the partial-output accumulation and the
            # deadline that raises with the guest pid were all dead code in
            # the battery, and the qemu timeout leg was really testing the
            # fake's own `subprocess.run(timeout=)`.
            # Whatever is still running is replaced by this command, so
            # end it first rather than dropping the handle.
            self._end_exec()
            emulated = self._emulate(arguments)
            if emulated is not None:
                self._exec_result = emulated
                self._exec_pid += 1
                return {"pid": self._exec_pid}
            if self._programs is not None and arguments["path"] in GNU_PROGRAMS:
                raise AssertionError(
                    "a GNU program must be emulated, never executed: %r"
                    % (arguments["path"],)
                )
            environment = os.environ.copy()
            for item in arguments.get("env", ()):
                key, value = item.split("=", 1)
                environment[key] = value
            input_data = arguments.get("input-data")
            payload = base64.b64decode(input_data) if input_data is not None else None
            process = subprocess.Popen(
                [arguments["path"], *arguments.get("arg", ())],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            if payload:
                process.stdin.write(payload)
            process.stdin.close()
            # And forget it. `communicate()` below flushes `stdin` if it is
            # still set, and flushing an already-closed pipe is a
            # `ValueError: flush of closed file` on Python 3.12 and older --
            # 3.13 tolerates it, which is why this only failed on the older
            # legs. The guest agent has sent all the input there is.
            process.stdin = None
            self._exec_process = process
            self._exec_output = None
            self._exec_pid += 1
            return {"pid": self._exec_pid}
        if command == "guest-exec-status":
            if self._exec_result is not None:
                code, stdout, stderr = self._exec_result
                return {
                    "exited": True,
                    "exitcode": code,
                    "out-data": base64.b64encode(stdout).decode("ascii"),
                    "err-data": base64.b64encode(stderr).decode("ascii"),
                }
            process = self._exec_process
            if process is None:
                raise AssertionError("guest-exec-status preceded guest-exec")
            if process.poll() is None:
                # Still running. qemu-ga answers the same way, and the
                # caller is expected to ask again.
                return {"exited": False}
            if self._exec_output is None:
                # Safe after exit for the small outputs this battery
                # produces; a fake is not a guest agent's ring buffer.
                self._exec_output = process.communicate()
            stdout, stderr = self._exec_output
            return {
                "exited": True,
                "exitcode": process.returncode,
                "out-data": base64.b64encode(stdout or b"").decode("ascii"),
                "err-data": base64.b64encode(stderr or b"").decode("ascii"),
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


#: The programs `PosixGuestPathHelper` runs. `GnuGuestPrograms` answers all
#: of them, so none may reach a real `Popen` against the host's filesystem.
GNU_PROGRAMS = frozenset(("stat", "find", "mkdir", "rm", "rmdir", "mv", "chmod"))

_TYPE_LETTERS = (
    (stat.S_ISREG, "f"),
    (stat.S_ISDIR, "d"),
    (stat.S_ISLNK, "l"),
    (stat.S_ISBLK, "b"),
    (stat.S_ISCHR, "c"),
    (stat.S_ISFIFO, "p"),
    (stat.S_ISSOCK, "s"),
)


class GnuGuestPrograms:
    """Interpret the GNU coreutils/findutils commands the POSIX helper runs.

    Modelled on coreutils 9.10 and findutils 4.10.0 (measured on Fedora 44):
    option parsing, output formats and error text are GNU's, so the helper is
    proven against a guest that behaves like one. `mv -n` onto an existing
    target exits 0 and changes nothing, as GNU does. Nothing here starts a
    process.
    """

    def __init__(self, sandbox: "_Sandbox") -> None:
        self.sandbox = sandbox

    def __call__(self, program: str, argv: Tuple[str, ...]):
        """Return `(exitcode, stdout, stderr)`, or `None` for another program."""
        if program not in GNU_PROGRAMS:
            return None
        try:
            return getattr(self, "_" + program)(list(argv))
        except OSError as exc:
            reason = {
                errno.ENOENT: "No such file or directory",
                errno.ENOTDIR: "Not a directory",
                errno.EEXIST: "File exists",
                errno.EISDIR: "Is a directory",
                errno.ENOTEMPTY: "Directory not empty",
                errno.EACCES: "Permission denied",
            }.get(exc.errno)
            if reason is None:
                raise
            text = "%s: cannot access '%s': %s\n" % (program, exc.filename, reason)
            return 1, b"", text.encode("utf-8")

    @staticmethod
    def _split(argv):
        """Options before the `--`, operands after it."""
        if "--" not in argv:
            raise AssertionError("a path operand must follow `--`: %r" % (argv,))
        cut = argv.index("--")
        return argv[:cut], argv[cut + 1 :]

    @staticmethod
    def _error(code: int, remote: str):
        return OSError(code, os.strerror(code), remote)

    def _stat_local(self, remote: str, local: Path, *, follow: bool = True):
        try:
            return os.stat(local) if follow else os.lstat(local)
        except FileNotFoundError:
            raise self._error(errno.ENOENT, remote) from None
        except NotADirectoryError:
            raise self._error(errno.ENOTDIR, remote) from None

    def _stat(self, argv):
        options, (remote,) = self._split(argv)
        value = self._stat_local(
            remote, self.sandbox.local(remote), follow="-L" in options
        )
        out = (
            options[options.index("-c") + 1]
            .replace("%f", format(value.st_mode, "x"))
            .replace("%s", str(value.st_size))
            .replace("%Y", str(int(value.st_mtime)))
        )
        return 0, (out + "\n").encode("utf-8"), b""

    def _find(self, argv):
        # `-H` follows the starting point when it is a link; without
        # `-mindepth 1` the starting point is printed first, as GNU find does.
        follow = argv[0] == "-H"
        if follow:
            argv = argv[1:]
        start = argv[0]
        if start.startswith("-"):
            # GNU find reads a leading `-x` as an expression and would run
            # it against `.`; the emulation refuses instead of running one.
            text = "find: unknown predicate `%s'\n" % start
            return 1, b"", text.encode("utf-8")
        fmt = argv[argv.index("-printf") + 1]
        directory = self.sandbox.local(start)
        try:
            top = os.stat(directory) if follow else os.lstat(directory)
        except FileNotFoundError:
            text = "find: '%s': No such file or directory\n" % start
            return 1, b"", text.encode("utf-8")

        def record(name, value):
            letter = next(
                (name for test, name in _TYPE_LETTERS if test(value.st_mode)), "U"
            )
            text = (
                fmt.replace("%f", name)
                .replace("%m", format(stat.S_IMODE(value.st_mode), "o"))
                .replace("%s", str(value.st_size))
                .replace("%T@", "%.10f" % value.st_mtime)
                .replace("%y", letter)
                .replace("\\0", "\0")
            )
            return text.encode("utf-8", "surrogateescape")

        records = bytearray()
        if "-mindepth" not in argv:
            records += record(posixpath.basename(start.rstrip("/")) or "/", top)
        if stat.S_ISDIR(top.st_mode):
            for child in sorted(directory.iterdir(), key=lambda item: item.name):
                records += record(child.name, child.lstat())
        return 0, bytes(records), b""

    def _mkdir(self, argv):
        options, (remote,) = self._split(argv)
        local = self.sandbox.local(remote)
        if os.path.lexists(local):
            raise self._error(errno.EEXIST, remote)
        if not local.parent.is_dir():
            raise self._error(errno.ENOENT, remote)
        local.mkdir()
        local.chmod(int(options[options.index("-m") + 1], 8))
        return 0, b"", b""

    def _rm(self, argv):
        options, (remote,) = self._split(argv)
        local = self.sandbox.local(remote)
        if not os.path.lexists(local):
            if "-f" in options:
                return 0, b"", b""
            raise self._error(errno.ENOENT, remote)
        if local.is_dir() and not local.is_symlink():
            raise self._error(errno.EISDIR, remote)
        local.unlink()
        return 0, b"", b""

    def _rmdir(self, argv):
        _, (remote,) = self._split(argv)
        local = self.sandbox.local(remote)
        value = self._stat_local(remote, local, follow=False)
        if not stat.S_ISDIR(value.st_mode):
            raise self._error(errno.ENOTDIR, remote)
        if any(local.iterdir()):
            raise self._error(errno.ENOTEMPTY, remote)
        local.rmdir()
        return 0, b"", b""

    def _mv(self, argv):
        options, (source, target) = self._split(argv)
        assert "-T" in options, "mv without -T moves INTO a directory target"
        src = self.sandbox.local(source)
        self._stat_local(source, src, follow=False)
        dst = self.sandbox.local(target)
        if os.path.lexists(dst):
            if "-n" in options:
                # GNU 9.10: the skip is silent and the status is 0.
                return 0, b"", b""
            if dst.is_dir():
                if any(dst.iterdir()):
                    raise self._error(errno.ENOTEMPTY, target)
                dst.rmdir()
        elif not dst.parent.is_dir():
            raise self._error(errno.ENOENT, target)
        src.replace(dst)
        return 0, b"", b""

    def _chmod(self, argv):
        options, (remote,) = self._split(argv)
        local = self.sandbox.local(remote)
        self._stat_local(remote, local)
        local.chmod(int(options[0], 8))
        return 0, b"", b""
