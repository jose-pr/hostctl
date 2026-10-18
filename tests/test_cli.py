import json
import subprocess
import sys
import types

import duho.mcp as duho_mcp
import pytest

from hostctl.shell._grammar import Exec
from hostctl.cli._root import Hostctl
from hostctl.cli import main
from hostctl.cli._common import split_operand


def test_every_connecting_command_takes_username_and_password(capsys):
    for command in ("run", "info", "ls", "cat", "cp", "shell", "deploy"):
        with pytest.raises(SystemExit) as exit_:
            main([command, "--help"])
        assert exit_.value.code == 0
        out = capsys.readouterr().out
        for flag in ("-u USERNAME", "--username", "-p PASSWORD", "--password"):
            assert flag in out, (command, flag)
        assert "--ask-password" not in out
    with pytest.raises(SystemExit) as exit_:
        main(["run", "--ask-password", "local:", "--", "echo"])
    assert exit_.value.code == 2


def test_run_propagates_status_and_streams(capsysbinary):
    code = main(
        [
            "run",
            "local:",
            "--",
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)",
        ]
    )
    captured = capsysbinary.readouterr()
    assert code == 7
    assert captured.out.strip() == b"out"
    assert captured.err.strip() == b"err"


def test_run_without_a_command_is_a_usage_failure(capsys):
    assert main(["run", "local:"]) == 125
    assert "run requires a command after --" in capsys.readouterr().err


def test_run_marks_the_operand_as_one_direct_command(monkeypatch):
    seen = []

    class _Host:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, *cmds, **options):
            seen.append(cmds)
            return subprocess.CompletedProcess(cmds, 0, b"", b"")

    monkeypatch.setattr("hostctl.cli._common.Host", lambda *args, **kwargs: _Host())

    # One `Exec`, not a leading path plus positionals: the operand is a single
    # direct command, and the program is passed verbatim so a bare name
    # resolves through the target's PATH.
    assert main(["run", "ssh://host", "--", "/bin/sh", "-c", "true"]) == 0
    assert seen == [(Exec("/bin/sh", "-c", "true"),)]


def test_cat_ls_cp_and_info(tmp_path, capsysbinary):
    source = tmp_path / "source.txt"
    source.write_bytes(b"value")
    target = tmp_path / "target.txt"

    assert main(["cp", str(source), str(target)]) == 0
    assert target.read_bytes() == b"value"

    capsysbinary.readouterr()
    assert main(["cat", "local:", str(target)]) == 0
    assert capsysbinary.readouterr().out == b"value"

    assert main(["ls", "local:", str(tmp_path)]) == 0
    listing = capsysbinary.readouterr().out.decode().splitlines()
    assert {"source.txt", "target.txt"} <= set(listing)

    assert main(["info", "local:"]) == 0
    assert "os_family" in json.loads(capsysbinary.readouterr().out)


def test_remote_operand_split_rejects_a_port_taken_as_the_path():
    assert split_operand("ssh://host:2222:/tmp/x") == ("ssh://host:2222", "/tmp/x")
    assert split_operand("ssh://host:/tmp/x") == ("ssh://host", "/tmp/x")
    assert split_operand("ssh://[::1]:2222:/tmp/x") == ("ssh://[::1]:2222", "/tmp/x")
    assert split_operand("ssh://user:pw@host:2222:/tmp/x") == (
        "ssh://user:pw@host:2222",
        "/tmp/x",
    )
    # A colon-free relative path is still a path, digits and all.
    assert split_operand("ssh://host:2222:22") == ("ssh://host:2222", "22")
    assert split_operand("./x") is None
    assert split_operand("C:\\x") is None

    # Each of these would otherwise split on the port colon, silently reaching
    # the default port with a relative path made of the port digits.
    for rejected in (
        "ssh://host:2222/x",
        "ssh://host:2222",
        "ssh://user:pw@host:2222/x",
        "ssh://[::1]:2222/x",
    ):
        with pytest.raises(ValueError, match="URI:PATH"):
            split_operand(rejected)


def test_cp_copies_through_the_source_path(monkeypatch):
    """The source path's own `copy()` runs, so a transport's native copy is
    not bypassed by a generic one."""
    from hostctl.cli._common import Credentialed

    calls = []

    class _Source:
        def copy(self, target, **options):
            calls.append((target, options))

    operands = {"a": _Source(), "b": object()}
    monkeypatch.setattr(
        Credentialed, "path_operand", lambda self, stack, value: operands[value]
    )

    assert main(["cp", "--overwrite", "--recursive", "a", "b"]) == 0
    assert calls == [(operands["b"], {"overwrite": True, "recursive": True})]


def test_cp_refuses_overwrite_without_flag(tmp_path, capsys):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_text("new")
    target.write_text("old")
    assert main(["cp", str(source), str(target)]) == 125
    assert target.read_text() == "old"


def test_failures_keep_their_status_and_their_kind(monkeypatch, capsys):
    def refuse(error):
        def host(*args, **kwargs):
            raise error

        monkeypatch.setattr("hostctl.cli._common.Host", host)

    refuse(ConnectionError("offline"))
    assert main(["info", "ssh://host"]) == 125
    assert capsys.readouterr().err == "hostctl: offline\n"

    refuse(PermissionError("auth failed"))
    assert main(["info", "ssh://host"]) == 126
    assert capsys.readouterr().err == "hostctl: permission denied: auth failed\n"

    refuse(FileNotFoundError("no such file"))
    assert main(["info", "ssh://host"]) == 127
    assert capsys.readouterr().err == "hostctl: not found: no such file\n"

    refuse(subprocess.TimeoutExpired("cmd", 5))
    assert main(["info", "ssh://host"]) == 125
    assert capsys.readouterr().err.startswith("hostctl: timed out: ")


def test_password_comes_from_environment_not_argv(monkeypatch, capsys):
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
    monkeypatch.setattr("hostctl.cli._common.Host", _Host)
    assert main(["info", "ssh://host"]) == 0
    assert seen == {"password": "environment-secret"}


def test_shell_uses_session_and_submits_input(monkeypatch, capsysbinary):
    import io

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

    monkeypatch.setattr("hostctl.cli._common.Host", lambda *args, **kwargs: _Host())
    monkeypatch.setattr("sys.stdin", io.StringIO("show version\n"))
    assert main(["shell", "serial:///loop%3A%2F%2F"]) == 0
    assert capsysbinary.readouterr().out == b"ready\n"
    assert events == [
        ("session", {"terminal": True}),
        ("send", "show version"),
        ("eof",),
        ("close",),
    ]


def test_an_exported_password_does_not_break_a_local_command(monkeypatch, capsys):
    """`HOSTCTL_PASSWORD` is ambient -- the guide says to export it -- so a
    scheme that declares no password must not fail on it."""
    monkeypatch.setenv("HOSTCTL_PASSWORD", "hunter2")

    code = main(["run", "local:", "--", sys.executable, "-c", "print('ok')"])

    assert code == 0
    assert "ok" in capsys.readouterr().out


def test_an_explicit_unknown_credential_still_fails_closed():
    """Filtering is for the ambient channel only: a name a config does not
    know, passed deliberately, is a typo worth reporting."""
    from hostctl.host._config import HostConfig

    with pytest.raises(ValueError, match="unknown credential argument"):
        HostConfig("local:", passwrd="typo")


def test_every_command_is_an_mcp_tool():
    names = {tool["name"] for tool in duho_mcp.describe_tools(Hostctl)}

    assert names == {
        f"hostctl.{name}"
        for name in ("run", "ls", "cat", "cp", "info", "shell", "deploy")
    }


def test_a_tool_call_returns_the_command_output_and_status():
    info = duho_mcp.call_tool(Hostctl, "hostctl.info", {"uri": "local:"})
    assert not info.get("isError")
    assert "os_family" in json.loads(info["content"][0]["text"])

    program = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    run = duho_mcp.call_tool(
        Hostctl,
        "hostctl.run",
        {"uri": "local:", "--": [sys.executable, "-c", program]},
    )
    text = run["content"][0]["text"]
    # The remote stderr arrives as bytes on a text-only captured stream.
    assert run["isError"] and "out" in text and "err" in text
    assert "exit code: 3" in text


def test_a_tool_takes_username_and_password_as_parameters():
    tools = {tool["name"]: tool for tool in duho_mcp.describe_tools(Hostctl)}
    for tool in tools.values():
        properties = tool["inputSchema"]["properties"]
        assert {"username", "password"} <= set(properties), tool["name"]
        assert "ask_password" not in properties


def test_without_duho_the_command_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "duho", None)

    with pytest.raises(SystemExit) as exit_:
        main(["info", "local:"])

    assert "hostctl[cli]" in str(exit_.value.code)


def test_importing_hostctl_does_not_import_the_command_line():
    probe = (
        "import sys, hostctl; "
        "loaded = {'hostctl.cli', 'duho'} & set(sys.modules); "
        "sys.exit(sorted(loaded) or 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("operand", ("ssh://deploy:dummyPW@nas", "winrm://u:dummyPW@h"))
def test_a_password_operand_is_refused_without_echoing_the_password(operand):
    """The userinfo colon is not the path separator, and no message quotes it."""
    with pytest.raises(ValueError) as caught:
        split_operand(operand)
    assert "dummyPW" not in str(caught.value)


def test_operand_errors_never_echo_a_password():
    for operand in ("ssh://deploy:dummyPW@nas:2222/x", "ssh://deploy:dummyPW@nas:"):
        with pytest.raises(ValueError) as caught:
            split_operand(operand)
        assert "dummyPW" not in str(caught.value)
    assert split_operand("ssh://deploy:dummyPW@nas:/srv/x") == (
        "ssh://deploy:dummyPW@nas",
        "/srv/x",
    )
    assert split_operand("ssh://deploy:dummyPW@nas:2222:/srv/x") == (
        "ssh://deploy:dummyPW@nas:2222",
        "/srv/x",
    )


def test_cp_with_a_password_operand_exits_125_and_does_not_print_it(capsys):
    code = main(["cp", "src.txt", "ssh://deploy:dummyPW@nas:2222/x"])

    assert code == 125
    assert "dummyPW" not in capsys.readouterr().err


def test_an_exported_password_is_not_offered_to_a_qemu_socket_scheme(monkeypatch):
    from hostctl.cli._common import usable_credentials

    assert usable_credentials("qemu+libvirt:///dom", {"password": "x"}) == {}
    assert usable_credentials("qga+unix:///s?domain=d", {"password": "x"}) == {}
    assert usable_credentials("qga+ssh://u@h/dom", {"password": "x"}) == {
        "password": "x"
    }


@pytest.fixture
def unreachable_ssh(monkeypatch):
    """SSH whose connect fails before anything is sent, with no DNS lookup."""

    def refuse(self, *args, **kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr(
        "hostctl.transports.ssh._transport._SSHTransport.connect", refuse
    )


def test_run_on_an_unreachable_host_exits_125_with_one_line(unreachable_ssh, capsys):
    assert main(["run", "ssh://nonexistent.invalid", "--", "true"]) == 125
    err = capsys.readouterr().err
    assert err.startswith("hostctl: ")
    assert len(err.strip().splitlines()) == 1
    assert "Traceback" not in err


def test_info_on_an_unreachable_host_exits_125_and_prints_no_identity(
    unreachable_ssh, capsys
):
    assert main(["info", "ssh://nonexistent.invalid"]) == 125
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("hostctl: ")
    assert "Traceback" not in captured.err


def test_the_library_info_of_an_unreachable_host_is_still_synthesized(unreachable_ssh):
    from hostctl.host._host import Host

    # Not entered as a context: `connect()` raises when nothing connected.
    host = Host("ssh://nonexistent.invalid")
    try:
        assert host.info().hostname == "nonexistent.invalid"
    finally:
        host.close()


def test_info_reports_a_refused_login_as_126(monkeypatch, capsys):
    from hostctl.host._info import HostInfo
    from hostctl.system._host import SystemHost

    def refused(self):
        return HostInfo(), False, [PermissionError("auth failed")]

    monkeypatch.setattr(SystemHost, "_collect_info", refused)
    monkeypatch.setattr(SystemHost, "connect", lambda self: None)
    assert main(["info", "ssh://host"]) == 126
    assert capsys.readouterr().err == "hostctl: permission denied: auth failed\n"


def test_info_exits_zero_when_a_provider_answers(capsys):
    assert main(["info", "local:"]) == 0
    assert "os_family" in json.loads(capsys.readouterr().out)


def test_every_epilog_uri_dispatches():
    from hostctl.host._config import HostConfig
    from hostctl.cli import _root

    lines = _root._EPILOG.split("connection URIs:\n", 1)[1].split("\n\n", 1)[0]
    uris = [line.split()[0] for line in lines.splitlines() if line.strip()]
    assert uris and not any(uri.startswith("psrp://") for uri in uris)
    for uri in uris:
        assert HostConfig(uri, **({"password": "x"} if "winrm" in uri else {}))

    operands = _root._EPILOG.split("remote path operands (cp):\n", 1)[1]
    examples = operands.split("\n\n", 1)[0].splitlines()[1:]
    for line in examples:
        for token in line.split():
            if "://" in token or token.startswith("local:/"):
                assert split_operand(token)


@pytest.mark.parametrize(
    "name, expected",
    (
        ("a\nb", "a\\x0ab"),
        ("\x1b]0;x\x07", "\\x1b]0;x\\x07"),
        ("tab\there", "tab\\x09here"),
        ("back\\slash", "back\\\\slash"),
        ("\udcff", "\\xff"),
        ("\u2028", "\\u2028"),
        ("\U000e0001", "\\U000e0001"),
        (" ", " "),
        ("na\u00efve caf\u00e9", "na\u00efve caf\u00e9"),
    ),
)
def test_display_name_escapes_what_a_peer_could_use_to_forge_output(name, expected):
    from hostctl.cli._common import display_name

    assert display_name(name) == expected


def test_ls_writes_one_line_per_name_whatever_the_name_holds(monkeypatch, capsysbinary):
    import contextlib

    from hostctl.cli import ls as lsmod

    class Child:
        def __init__(self, name):
            self.name = name

    class Fake:
        def path(self, _):
            class P:
                def iterdir(self):
                    return iter(
                        [Child("report.txt"), Child("\x1b[2Jclean\nfake-entry.txt")]
                    )

            return P()

    @contextlib.contextmanager
    def connect(self):
        yield Fake()

    monkeypatch.setattr(lsmod.Ls, "connect", connect)

    assert main(["ls", "ssh://hostile", "/srv"]) == 0
    out = capsysbinary.readouterr().out
    assert out == b"report.txt\n\\x1b[2Jclean\\x0afake-entry.txt\n"
    assert not [byte for byte in out if byte < 0x20 and byte != 0x0A]
