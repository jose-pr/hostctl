"""Guest-side metadata and namespace operations for QGA paths.

QGA's file RPCs move bytes and nothing else: there is no `guest-file-stat`,
no listing, no rename. `QgaPathBackend` therefore gates `stat`, `scandir`
and every mutation on a `GuestPathHelper`, and without one the path
provider reports `degraded`. This module supplies that helper by running
ordinary programs in the guest through `guest-exec`.

**Positively probed, never assumed.** `stat -c`, `find -printf` and
`ls --time-style` are GNU spellings that busybox and the BSDs do not share,
and a helper that assumes GNU and silently mis-parses is worse than no
helper at all. `PosixGuestPathHelper.probe()` runs one decisive command and
returns `None` when the guest cannot answer it, which leaves the backend in
exactly the degraded mode it has today rather than turning a working
read/write path into an error.

Measured on Fedora 44 (GNU coreutils 9.10, findutils 4.10.0):

- `stat -c %f -- /` answers `41ed` -- hex, and a directory. A guest whose
  `stat` does not take `-c` fails or answers something that is not hex,
  which is what the probe tests.
- `find <dir> -maxdepth 1 -mindepth 1 -printf '%f\\0%m\\0%s\\0%T@\\0%y\\0'`
  round-trips a filename containing a NEWLINE, which every line-oriented
  listing loses. That is why the fields are NUL-separated rather than
  parsed out of `ls`.
- `stat -L` dereferences (`a1ff` for the link, `81a4` for its target), so
  `follow_symlinks` is a flag rather than a second implementation.
"""

from __future__ import annotations

import base64
import stat as _stat
import subprocess
import typing

from pathlib_next.utils.stat import FileStat

from ..executor._qga import guest_oserror

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

    Every path is passed as its own argv element and every command is
    terminated with `--`, so a path is never interpolated into shell text
    and a leading `-` is data rather than an option.
    """

    #: What `probe()` runs, and what a usable guest answers.
    probe_command = ("stat", "-c", "%f", "--", "/")

    def __init__(self, run: GuestRunner) -> None:
        self._run = run

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
        argv = [
            "find",
            path,
            "-maxdepth",
            "1",
            "-mindepth",
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
        entries = []
        for index in range(0, len(fields), 5):
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
        argv = ["mv", "-T"] if replace else ["mv", "-T", "-n"]
        self._execute([*argv, "--", path, target], path=path)

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
    #: script travels the same `-EncodedCommand` route, so a guest that
    #: answers this can run them.
    probe_script = "exit 0"

    def __init__(self, backend: object) -> None:
        self._backend = backend

    @classmethod
    def runner(cls, run: GuestRunner) -> typing.Callable[..., typing.Any]:
        """Adapt a `guest-exec` runner to what `WinRMPathBackend` calls.

        `-EncodedCommand` rather than `-Command`: the script becomes
        UTF-16LE base64, which carries no character that any argv or shell
        layer between here and PowerShell would touch.
        """

        def run_script(script, check=False, encoding="utf-8", **options):
            del options
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            return run(
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
                capture_output=True,
                check=check,
                encoding=encoding,
            )

        return run_script

    @classmethod
    def probe(cls, run: GuestRunner) -> typing.Optional["WindowsGuestPathHelper"]:
        from ._winrm import WinRMPathBackend

        run_script = cls.runner(run)
        try:
            result = run_script(cls.probe_script)
        except Exception:
            return None
        if getattr(result, "returncode", 1) != 0:
            return None
        return cls(WinRMPathBackend(run_script))

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


__all__ = [
    "PosixGuestPathHelper",
    "WindowsGuestPathHelper",
    "GuestRunner",
]
