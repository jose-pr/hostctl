"""The guest-side helper QGA paths need for metadata and mutations.

QGA's file RPCs move bytes and nothing else, so `stat`, `scandir` and every
namespace change go through a helper that runs ordinary programs in the
guest. The guest is faked at the `guest-exec` layer here; the commands and
their output shapes were measured against a real GNU userland (Fedora 44,
coreutils 9.10, findutils 4.10.0) and a real `powershell.exe`.
"""

from __future__ import annotations

import subprocess

import pytest

from hostctl.host._qga_helper import PosixGuestPathHelper, WindowsGuestPathHelper


class _Guest:
    """A fake `guest-exec` runner with scripted answers per program."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.calls = []

    def __call__(self, program, *args, capture_output=True, check=False, **options):
        self.calls.append((str(program), tuple(str(value) for value in args)))
        answer = self.answers.get(str(program), (0, b"", b""))
        if callable(answer):
            answer = answer(args)
        returncode, stdout, stderr = answer
        return subprocess.CompletedProcess([program, *args], returncode, stdout, stderr)


def test_the_probe_accepts_a_gnu_guest():
    """`stat -c %f -- /` answers hex, and `/` is a directory.

    Measured on Fedora 44: `41ed`.
    """
    guest = _Guest({"stat": (0, b"41ed\n", b"")})

    helper = PosixGuestPathHelper.probe(guest)

    assert isinstance(helper, PosixGuestPathHelper)
    assert guest.calls == [("stat", ("-c", "%f", "--", "/"))]


@pytest.mark.parametrize(
    ("label", "answer"),
    (
        # busybox `stat` has no `-c`.
        ("no -c", (1, b"", b"stat: invalid option -- 'c'")),
        # A `stat` that printed its own report rather than the format.
        ("not hex", (0, b"  File: /\n  Size: 4096\n", b"")),
        # Hex, but not a directory: the guest answered a different question.
        ("not a directory", (0, b"81a4\n", b"")),
    ),
)
def test_the_probe_declines_rather_than_guessing(label, answer):
    """A helper that assumes GNU and mis-parses is worse than no helper.

    `None` leaves the backend in exactly the degraded mode it has today --
    reads and writes keep working -- rather than turning a working path
    into an error.
    """
    assert PosixGuestPathHelper.probe(_Guest({"stat": answer})) is None


def test_stat_reads_the_three_fields_it_asks_for():
    guest = _Guest({"stat": (0, b"81a4 8 1790043337\n", b"")})
    helper = PosixGuestPathHelper(guest)

    value = helper.stat("/etc/hostname")

    assert value.st_mode == 0o100644
    assert value.st_size == 8
    assert value.st_mtime == 1790043337
    assert guest.calls[-1] == (
        "stat",
        ("-L", "-c", "%f %s %Y", "--", "/etc/hostname"),
    )


def test_stat_without_following_drops_the_dereference_flag():
    guest = _Guest({"stat": (0, b"a1ff 13 1790043337\n", b"")})

    value = PosixGuestPathHelper(guest).stat("/etc/link", follow_symlinks=False)

    assert "-L" not in guest.calls[-1][1]
    assert value.st_mode == 0o120777


def test_scandir_survives_a_newline_in_a_filename():
    """The reason the fields are NUL-separated rather than parsed from `ls`.

    Measured: `find -printf` round-trips `we\\nird`, which every
    line-oriented listing loses.
    """
    payload = b"\0".join(
        [
            b"sub",
            b"755",
            b"40",
            b"1790043376.3",
            b"d",
            b"we\nird",
            b"644",
            b"1",
            b"1790043376.3",
            b"f",
            b"link",
            b"777",
            b"13",
            b"1790043376.3",
            b"l",
            b"",
        ]
    )
    guest = _Guest({"find": (0, payload, b"")})

    entries = dict(PosixGuestPathHelper(guest).scandir("/tmp/x"))

    assert set(entries) == {"sub", "we\nird", "link"}
    assert entries["sub"].st_mode == 0o40755
    assert entries["we\nird"].st_mode == 0o100644
    assert entries["link"].st_mode == 0o120777
    assert entries["we\nird"].st_size == 1


def test_the_scandir_format_travels_as_an_escape_not_a_nul():
    """argv crosses `execve`, which ends a C string at the first NUL.

    `find` interprets the `\\0` escape itself; a real NUL could never reach
    it. Measured: sent as an escape, find emits the separator.
    """
    guest = _Guest({"find": (0, b"", b"")})

    PosixGuestPathHelper(guest).scandir("/tmp/x")

    fmt = guest.calls[-1][1][-1]
    assert "\0" not in fmt
    assert fmt.count(chr(92) + "0") == 5


def test_a_short_scandir_record_is_refused():
    """Half a record must not be guessed into a FileStat."""
    guest = _Guest({"find": (0, b"name\0644\0", b"")})

    with pytest.raises(OSError, match="whole number of records"):
        PosixGuestPathHelper(guest).scandir("/tmp/x")


@pytest.mark.parametrize(
    ("call", "expected"),
    (
        (lambda h: h.mkdir("/tmp/d", 0o700), ("mkdir", ("-m", "0700", "--", "/tmp/d"))),
        (lambda h: h.unlink("/tmp/f"), ("rm", ("--", "/tmp/f"))),
        (
            lambda h: h.unlink("/tmp/f", missing_ok=True),
            ("rm", ("-f", "--", "/tmp/f")),
        ),
        (lambda h: h.rmdir("/tmp/d"), ("rmdir", ("--", "/tmp/d"))),
        (
            lambda h: h.rename("/a", "/b"),
            ("mv", ("-T", "-n", "--", "/a", "/b")),
        ),
        (
            lambda h: h.rename("/a", "/b", replace=True),
            ("mv", ("-T", "--", "/a", "/b")),
        ),
        (lambda h: h.chmod("/tmp/f", 0o640), ("chmod", ("0640", "--", "/tmp/f"))),
    ),
)
def test_every_mutation_passes_paths_as_argv_after_a_double_dash(call, expected):
    """`--` so a path beginning with `-` is data, never an option.

    And `mv -T` so a rename onto an existing directory is a rename, not a
    move INTO it -- which is what `mv a b` silently becomes otherwise.
    """
    guest = _Guest()

    call(PosixGuestPathHelper(guest))

    assert guest.calls[-1] == expected


def test_a_failing_guest_command_becomes_a_filesystem_error():
    guest = _Guest(
        {"stat": (1, b"", b"stat: cannot statx '/nope': No such file or directory")}
    )

    with pytest.raises(FileNotFoundError):
        PosixGuestPathHelper(guest).stat("/nope")


def test_a_symlinks_own_mode_is_refused_rather_than_redirected():
    """GNU chmod has no `-h`, and a link's mode is not meaningful on Linux."""
    with pytest.raises(NotImplementedError, match="symlink"):
        PosixGuestPathHelper(_Guest()).chmod("/tmp/l", 0o600, follow_symlinks=False)


# -- the Windows half -----------------------------------------------------


def test_the_windows_helper_sends_an_encoded_command():
    """`-EncodedCommand` carries no character an argv layer would touch."""
    import base64

    guest = _Guest({"powershell.exe": (0, b"", b"")})

    helper = WindowsGuestPathHelper.probe(guest)

    assert isinstance(helper, WindowsGuestPathHelper)
    program, args = guest.calls[-1]
    assert program == "powershell.exe"
    assert "-EncodedCommand" in args
    encoded = args[args.index("-EncodedCommand") + 1]
    assert base64.b64decode(encoded).decode("utf-16-le") == "exit 0"


def test_the_windows_probe_declines_a_guest_without_powershell():
    assert (
        WindowsGuestPathHelper.probe(_Guest({"powershell.exe": (1, b"", b"")})) is None
    )


def test_the_windows_helper_refuses_what_windows_cannot_express():
    """Refused, not accepted and ignored.

    A replacing rename would lose the destination on a guest where the
    underlying `[IO.File]::Move` happened to succeed, and a symlink's own
    mode has no NTFS meaning.
    """
    helper = WindowsGuestPathHelper(object())

    with pytest.raises(NotImplementedError, match="replace"):
        helper.rename("a", "b", replace=True)
    with pytest.raises(NotImplementedError, match="symlink"):
        helper.chmod("a", 0o600, follow_symlinks=False)


# -- wiring ---------------------------------------------------------------


class _QgaTransport:
    """A guest agent that runs `guest-exec` against a scripted guest."""

    def __init__(self, guest, *, osinfo=None, commands=()):
        self.guest = guest
        self.osinfo = osinfo
        self.commands = tuple(commands)
        self._result = None

    def execute(self, request, timeout=None):
        import base64

        command = request["execute"]
        arguments = request.get("arguments", {})
        if command == "guest-ping":
            return {}
        if command == "guest-info":
            names = (
                "guest-exec",
                "guest-exec-status",
                "guest-file-open",
                "guest-file-read",
                "guest-file-write",
                "guest-file-close",
            ) + self.commands
            return {
                "supported_commands": [
                    {"name": name, "enabled": True} for name in names
                ]
            }
        if command == "guest-get-osinfo":
            return self.osinfo or {}
        if command == "guest-exec":
            self._result = self.guest(arguments["path"], *arguments.get("arg", ()))
            return {"pid": 1}
        if command == "guest-exec-status":
            result = self._result
            return {
                "exited": True,
                "exitcode": result.returncode,
                "out-data": base64.b64encode(result.stdout or b"").decode("ascii"),
                "err-data": base64.b64encode(result.stderr or b"").decode("ascii"),
            }
        raise AssertionError(command)

    def close(self):
        pass


def _host(transport, **options):
    from hostctl.host.qemu import QemuConfig, QemuHost

    return QemuHost(QemuConfig("guest", transport_factory=lambda: transport, **options))


def test_a_probed_guest_gets_a_real_path_surface():
    """The helper is built during discovery, so `path()` has one by default.

    Before this, hostctl shipped no helper at all: a caller had to write one
    and pass `QemuConfig(path_helper=...)`, and every QGA path reported
    `degraded`.
    """
    guest = _Guest(
        {
            "stat": (0, b"41ed\n", b""),
            "find": (0, b"one.txt\x00644\x007\x001790043376.3\x00f\x00", b""),
        }
    )
    transport = _QgaTransport(
        guest,
        osinfo={"id": "ubuntu", "kernel-name": "Linux"},
        commands=("guest-get-osinfo",),
    )
    host = _host(transport)

    entries = list(host.path("/srv").iterdir())

    assert [entry.name for entry in entries] == ["one.txt"]


def test_a_guest_that_fails_the_probe_keeps_working_without_a_helper():
    """Degraded, not broken: reads and writes never needed a helper.

    Turning a failed probe into an error would take `path()` away from every
    busybox guest that has it today.
    """
    guest = _Guest({"stat": (1, b"", b"stat: invalid option -- 'c'")})
    transport = _QgaTransport(
        guest,
        osinfo={"id": "alpine", "kernel-name": "Linux"},
        commands=("guest-get-osinfo",),
    )
    host = _host(transport)

    path = host.path("/srv/file")

    with pytest.raises(NotImplementedError):
        path.stat()


def test_the_probe_runs_once_however_many_paths_are_built():
    """`None` is a real answer, not "ask again next time"."""
    guest = _Guest({"stat": (1, b"", b"nope")})
    transport = _QgaTransport(
        guest,
        osinfo={"id": "alpine", "kernel-name": "Linux"},
        commands=("guest-get-osinfo",),
    )
    host = _host(transport)

    for _ in range(3):
        host.path("/srv/file")

    assert sum(1 for call in guest.calls if call[0] == "stat") == 1


def test_an_explicit_helper_is_not_second_guessed():
    """`QemuConfig(path_helper=...)` overrides the probe entirely."""
    guest = _Guest({"stat": (0, b"41ed\n", b"")})
    transport = _QgaTransport(
        guest,
        osinfo={"id": "ubuntu", "kernel-name": "Linux"},
        commands=("guest-get-osinfo",),
    )
    supplied = PosixGuestPathHelper(_Guest({"stat": (0, b"41ed 0 0\n", b"")}))
    host = _host(transport, path_helper=supplied)

    host.path("/srv")

    assert host._path_backend.helper is supplied
    # The probe never ran: the caller already answered the question.
    assert not any(call[0] == "stat" for call in guest.calls)


def test_a_guest_that_names_no_family_gets_no_helper():
    """A helper for the wrong family would run commands the guest lacks."""
    guest = _Guest({"stat": (0, b"41ed\n", b"")})
    transport = _QgaTransport(guest)  # no osinfo, no family-exclusive command
    host = _host(transport, path_flavor="posix")

    host.path("/srv")

    assert host._path_backend.helper is None
    assert not guest.calls
