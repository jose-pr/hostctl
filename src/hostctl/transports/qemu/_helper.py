"""Guest path helpers: metadata and namespace operations run as guest programs."""

from __future__ import annotations


import subprocess
import typing


from pathapi import FileStat


from ...paths._utime import Touch
from ._qga import guest_oserror


import errno
import os
import stat as _stat


class GuestPathHelper(typing.Protocol):
    """Positively probed OS helper operations not supplied by QGA file RPCs."""

    def stat(self, path: str, *, follow_symlinks: bool = True) -> FileStat: ...

    def scandir(self, path: str) -> typing.Iterable[typing.Tuple[str, FileStat]]: ...

    def mkdir(self, path: str, mode: int) -> None: ...

    def unlink(self, path: str, *, missing_ok: bool = False) -> None: ...

    def rmdir(self, path: str) -> None: ...

    def rename(self, path: str, target: str, *, replace: bool = False) -> None: ...

    def chmod(self, path: str, mode: int, *, follow_symlinks: bool = True) -> None: ...

    def utime(
        self,
        path: str,
        *,
        atime: typing.Optional[float] = None,
        mtime: typing.Optional[float] = None,
    ) -> None: ...


#: Script bytes one `guest-exec` call carries as its `input-data`; a larger
#: write is split across calls.
_GUEST_EXEC_SCRIPT_BUDGET = 12_000


#: `find -printf`'s type letters, in `stat` terms.

_FILE_TYPES = {
    "f": _stat.S_IFREG,
    "d": _stat.S_IFDIR,
    "l": _stat.S_IFLNK,
    "b": _stat.S_IFBLK,
    "c": _stat.S_IFCHR,
    "p": _stat.S_IFIFO,
    "s": _stat.S_IFSOCK,
}


#: One `find -printf` record: name, octal permissions, size, mtime, type.
#:
#: The separator is the two characters backslash and 0, NOT a NUL byte:
#: `find` interprets the escape itself, and a real NUL could not reach
#: it anyway -- argv crosses `execve`, which ends a C string at the
#: first NUL. Sent as an escape it arrives intact and find emits it.
_SCANDIR_FORMAT = "%f\\0%m\\0%s\\0%T@\\0%y\\0"


GuestRunner = typing.Callable[..., subprocess.CompletedProcess]


class PosixGuestPathHelper:
    """Metadata and namespace operations for a GNU-coreutils guest.

    Every path is passed as its own argv element and every command but
    `find` is terminated with `--`, so a path is never interpolated into
    shell text and a leading `-` is data rather than an option. `find`
    accepts no `--` before a starting point; it gets an absolute path or one
    anchored with `./`.
    """

    #: What `probe()` runs, and what a usable guest answers.
    probe_command = ("stat", "-c", "%f", "--", "/")

    def __init__(self, run: GuestRunner) -> None:
        self._run = run
        self._touch = Touch(self._run_touch)

    # -- construction ----------------------------------------------------

    @classmethod
    def probe(cls, run: GuestRunner) -> typing.Optional["PosixGuestPathHelper"]:
        """Return a helper, or `None` when this guest cannot support one.

        `None` rather than an exception: the backend's read and write RPCs
        work without a helper, and a guest running busybox should keep them
        rather than lose `path()` entirely.
        """
        helper = cls(run)
        try:
            result = helper._execute(cls.probe_command, path="/")
        except Exception:
            return None
        text = helper._text(result).strip()
        try:
            mode = int(text, 16)
        except ValueError:
            return None
        # `/` is a directory on every POSIX guest. A `stat` that ignored
        # `-c` and printed its own report would not parse as hex at all,
        # but one that answered a plausible number for the wrong question
        # still would -- so check the answer means what it should.
        return helper if _stat.S_ISDIR(mode) else None

    # -- plumbing --------------------------------------------------------

    def _execute(
        self, argv: typing.Sequence[str], *, path: str
    ) -> subprocess.CompletedProcess:
        program, *arguments = argv
        result = self._run(
            program,
            *arguments,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise guest_oserror(
                _GuestCommandError(self._text(result, stream="stderr")), path
            )
        return result

    def _run_touch(self, argv: typing.Sequence[str]) -> typing.Tuple[int, str]:
        program, *arguments = argv
        result = self._run(program, *arguments, capture_output=True, check=False)
        return result.returncode, self._text(result, stream="stderr")

    @staticmethod
    def _text(result: subprocess.CompletedProcess, stream: str = "stdout") -> str:
        value = getattr(result, stream, None) or b""
        if isinstance(value, bytes):
            return value.decode("utf-8", "surrogateescape")
        return value

    @staticmethod
    def _raw(result: subprocess.CompletedProcess) -> bytes:
        value = result.stdout or b""
        if isinstance(value, str):
            return value.encode("utf-8", "surrogateescape")
        return value

    # -- metadata --------------------------------------------------------

    def stat(self, path: str, *, follow_symlinks: bool = True) -> FileStat:
        argv = ["stat"]
        if follow_symlinks:
            argv.append("-L")
        argv += ["-c", "%f %s %Y", "--", path]
        fields = self._text(self._execute(argv, path=path)).split()
        if len(fields) != 3:
            raise OSError(f"guest stat returned {fields!r} for {path!r}")
        mode, size, mtime = fields
        return FileStat(
            st_mode=int(mode, 16),
            st_size=int(size),
            st_mtime=int(mtime),
        )

    def scandir(self, path: str) -> typing.List[typing.Tuple[str, FileStat]]:
        # `find` takes no `--` before its starting points, so a relative path
        # is anchored with `./` instead: `-delete` would otherwise be read as
        # an expression and run against the current directory. Its first
        # record is the starting point itself (`-H` follows it if it is a
        # link), which is how a regular file is told from an empty directory.
        argv = [
            "find",
            "-H",
            path if path.startswith("/") else "./" + path,
            "-maxdepth",
            "1",
            "-printf",
            _SCANDIR_FORMAT,
        ]
        payload = self._raw(self._execute(argv, path=path))
        fields = payload.split(b"\0")
        # A trailing NUL leaves one empty field; anything else is a short
        # record, which must not be guessed at.
        if fields and not fields[-1]:
            fields.pop()
        if len(fields) % 5:
            raise OSError(
                f"guest find returned {len(fields)} fields for {path!r}, "
                "which is not a whole number of records"
            )
        if not fields:
            raise OSError(f"guest find printed no record for {path!r}")
        if fields[4] != b"d":
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), path)
        entries = []
        for index in range(5, len(fields), 5):
            name, mode, size, mtime, kind = fields[index : index + 5]
            entries.append(
                (
                    name.decode("utf-8", "surrogateescape"),
                    FileStat(
                        st_mode=(
                            int(mode, 8)
                            | _FILE_TYPES.get(
                                kind.decode("ascii", "replace"), _stat.S_IFREG
                            )
                        ),
                        st_size=int(size),
                        # `%T@` is seconds with a fractional part.
                        st_mtime=int(float(mtime)),
                    ),
                )
            )
        return entries

    # -- namespace -------------------------------------------------------

    def mkdir(self, path: str, mode: int) -> None:
        self._execute(
            ["mkdir", "-m", format(mode & 0o7777, "04o"), "--", path], path=path
        )

    def unlink(self, path: str, *, missing_ok: bool = False) -> None:
        argv = ["rm", "-f", "--", path] if missing_ok else ["rm", "--", path]
        self._execute(argv, path=path)

    def rmdir(self, path: str) -> None:
        self._execute(["rmdir", "--", path], path=path)

    def rename(self, path: str, target: str, *, replace: bool = False) -> None:
        # `-T` treats the destination as a name, never as a directory to move
        # INTO: without it `mv a b` silently becomes `mv a b/a` whenever `b`
        # happens to be a directory, which is not what a rename means.
        if replace:
            self._execute(["mv", "-T", "--", path, target], path=path)
            return
        # `-n` skips an existing target and, on GNU mv, still exits 0, so the
        # status proves nothing: a source that is still there was not moved.
        self._execute(["mv", "-T", "-n", "--", path, target], path=path)
        try:
            self._execute(["stat", "-c", "%f", "--", path], path=path)
        except FileNotFoundError:
            return
        raise FileExistsError(errno.EEXIST, "File exists", target)

    def chmod(self, path: str, mode: int, *, follow_symlinks: bool = True) -> None:
        if not follow_symlinks:
            # GNU chmod has no `-h`, and a symlink's own mode is not
            # meaningful on Linux. Refusing beats silently changing the
            # target's mode instead.
            raise NotImplementedError(
                "a guest chmod cannot act on a symlink itself; GNU chmod "
                "has no -h and a link's mode is not meaningful on Linux"
            )
        self._execute(["chmod", format(mode & 0o7777, "04o"), "--", path], path=path)

    def utime(
        self,
        path: str,
        *,
        atime: typing.Optional[float] = None,
        mtime: typing.Optional[float] = None,
    ) -> None:
        """Set the times with `touch`, following a link as `touch` does.

        `touch -c` exits 0 for a missing path, so `stat` proves it exists
        first. GNU keeps nanoseconds; busybox whole seconds (see `Touch`).
        """
        self._execute(["stat", "-L", "-c", "%f", "--", path], path=path)
        self._touch.utime(
            path,
            atime=atime,
            mtime=mtime,
            error=lambda text: guest_oserror(_GuestCommandError(text), path),
        )


class _GuestCommandError(Exception):
    """A guest command's stderr, shaped for `guest_oserror` to classify."""

    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.description = description


class WindowsGuestPathHelper:
    """A `GuestPathHelper` for a Windows guest, over `guest-exec`.

    `WinRMPathBackend` is already a script-driven path backend whose only
    dependency is a callable returning a `CompletedProcess` -- its scripts
    are the ones `tests/test_winrm_powershell_live.py` drives through a real
    `powershell.exe`. A Windows guest reached through `guest-exec` supplies
    exactly that callable, so those scripts are reused rather than written a
    second time against the same PowerShell.

    The adapter is thin but not silent: where the `GuestPathHelper` protocol
    asks for something Windows cannot express, it refuses rather than
    accepting the argument and ignoring it.
    """

    #: The cheapest proof that a guest has PowerShell at all. Every later
    #: script travels the same stdin route, so a guest that answers this can
    #: run them.
    probe_script = "exit 0"

    def __init__(self, backend: object) -> None:
        self._backend = backend

    @classmethod
    def runner(cls, run: GuestRunner) -> typing.Callable[..., typing.Any]:
        """Adapt a `guest-exec` runner to what `WinRMPathBackend` calls.

        The script travels on stdin (`-Command -`), so it never appears on
        the command line, which endpoint-protection heuristics scan. The
        trailing blank line closes the last statement; `WinRMPathBackend`
        scripts are ASCII, so the stdin code page does not matter.
        """

        def run_script(script, check=False, encoding="utf-8", **options):
            del options
            return run(
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "-",
                input=script.encode("utf-8") + b"\r\n\r\n",
                capture_output=True,
                check=check,
                encoding=encoding,
            )

        return run_script

    @classmethod
    def probe(cls, run: GuestRunner) -> typing.Optional["WindowsGuestPathHelper"]:
        from ..winrm._path import WinRMPathBackend

        run_script = cls.runner(run)
        try:
            result = run_script(cls.probe_script)
        except Exception:
            return None
        if getattr(result, "returncode", 1) != 0:
            return None
        return cls(
            WinRMPathBackend(run_script, max_script_bytes=_GUEST_EXEC_SCRIPT_BUDGET)
        )

    # -- metadata --------------------------------------------------------

    def stat(self, path: str, *, follow_symlinks: bool = True) -> FileStat:
        return self._backend.stat(path, follow_symlinks=follow_symlinks)

    def scandir(self, path: str) -> typing.List[typing.Tuple[str, FileStat]]:
        return self._backend.scandir(path)

    # -- namespace -------------------------------------------------------

    def mkdir(self, path: str, mode: int) -> None:
        self._backend.mkdir(path)
        # `0o777` is pathlib's "no opinion" default. Anything else is a
        # request, and on NTFS the only bit that maps is the write bit --
        # which `chmod` below is honest about.
        if mode & 0o777 != 0o777:
            self._backend.chmod(path, mode)

    def unlink(self, path: str, *, missing_ok: bool = False) -> None:
        self._backend.unlink(path, missing_ok=missing_ok)

    def rmdir(self, path: str) -> None:
        self._backend.rmdir(path)

    def rename(self, path: str, target: str, *, replace: bool = False) -> None:
        if replace:
            # The backend's `[IO.File]::Move($p,$t)` fails when the target
            # exists. Pretending otherwise would lose the destination's
            # contents on a guest where it happened to succeed.
            raise NotImplementedError(
                "a Windows guest rename cannot replace an existing target"
            )
        self._backend.rename(path, target)

    def chmod(self, path: str, mode: int, *, follow_symlinks: bool = True) -> None:
        if not follow_symlinks:
            raise NotImplementedError(
                "a Windows guest chmod cannot act on a symlink itself"
            )
        # Only the write bit maps: NTFS has no POSIX mode, and
        # `WinRMPathBackend.chmod` sets or clears the read-only attribute.
        self._backend.chmod(path, mode)

    def utime(
        self,
        path: str,
        *,
        atime: typing.Optional[float] = None,
        mtime: typing.Optional[float] = None,
    ) -> None:
        self._backend.utime(path, atime=atime, mtime=mtime)


__all__ = [
    "PosixGuestPathHelper",
    "WindowsGuestPathHelper",
    "GuestRunner",
]
