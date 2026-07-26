import io
import json
import subprocess
import types
from pathlib import PurePosixPath

import pytest

from hostctl._cli import _parser, main


def test_cli_has_no_password_argument():
    help_text = _parser().format_help()
    assert "--password" not in help_text
    with pytest.raises(SystemExit):
        _parser().parse_args(["run", "--password", "secret", "local:", "--", "echo"])


def test_run_propagates_status_and_streams():
    stdout = io.BytesIO()
    stderr = io.BytesIO()
    code = main(
        [
            "run",
            "local:",
            "--",
            __import__("sys").executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)",
        ],
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 7
    assert stdout.getvalue().strip() == b"out"
    assert stderr.getvalue().strip() == b"err"


def test_run_marks_direct_command_with_target_shell_path_flavour(monkeypatch):
    seen = []

    class _Host:
        from hostctl import POSIX_SHELL

        shell_flavour = POSIX_SHELL

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, *cmds, **options):
            seen.append(cmds)
            return subprocess.CompletedProcess(cmds, 0, b"", b"")

    monkeypatch.setattr("hostctl._cli.Host", lambda *args, **kwargs: _Host())

    assert main(["run", "ssh://host", "--", "/bin/sh", "-c", "true"]) == 0
    assert seen == [(PurePosixPath("/bin/sh"), "-c", "true")]


def test_cat_ls_cp_and_info(tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"value")
    target = tmp_path / "target.txt"

    assert main(["cp", str(source), str(target)]) == 0
    assert target.read_bytes() == b"value"

    stdout = io.BytesIO()
    assert main(["cat", "local:", str(target)], stdout=stdout) == 0
    assert stdout.getvalue() == b"value"

    listing = io.StringIO()
    assert main(["ls", "local:", str(tmp_path)], stdout=listing) == 0
    assert {"source.txt", "target.txt"} <= set(listing.getvalue().splitlines())

    info = io.StringIO()
    assert main(["info", "local:"], stdout=info) == 0
    assert "os_family" in json.loads(info.getvalue())


def test_cp_refuses_overwrite_without_flag(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_text("new")
    target.write_text("old")
    error = io.StringIO()
    assert main(["cp", str(source), str(target)], stderr=error) == 125
    assert target.read_text() == "old"


def test_connection_failures_use_transport_status(monkeypatch):
    monkeypatch.setattr(
        "hostctl._cli.Host",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("offline")),
    )
    error = io.StringIO()
    assert main(["info", "ssh://host"], stderr=error) == 125
    assert "offline" in error.getvalue()


def test_password_comes_from_environment_not_argv(monkeypatch):
    seen = {}

    class _Host:
        def __init__(self, uri, **credentials):
            seen.update(credentials)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def info(self):
            return types.SimpleNamespace()

    monkeypatch.setenv("HOSTCTL_PASSWORD", "environment-secret")
    monkeypatch.setattr("hostctl._cli.Host", _Host)
    assert main(["info", "ssh://host"], stdout=io.StringIO()) == 0
    assert seen == {"password": "environment-secret"}


def test_shell_uses_session_and_submits_input(monkeypatch):
    events = []

    class _Session:
        def __init__(self):
            self.reads = iter((b"ready\n", b""))

        def read(self, size=-1):
            return next(self.reads, b"")

        def send(self, value):
            events.append(("send", value))

        def send_eof(self):
            events.append(("eof",))

        def close(self):
            events.append(("close",))

    session = _Session()

    class _Host:
        shell = types.SimpleNamespace(
            session=lambda **options: (events.append(("session", options)) or session)
        )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("hostctl._cli.Host", lambda *args, **kwargs: _Host())
    monkeypatch.setattr("hostctl._cli.sys.stdin", io.StringIO("show version\n"))
    output = io.BytesIO()
    assert main(["shell", "serial:///loop%3A%2F%2F"], stdout=output) == 0
    assert output.getvalue() == b"ready\n"
    assert events == [
        ("session", {"terminal": True}),
        ("send", "show version"),
        ("eof",),
        ("close",),
    ]
