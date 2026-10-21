"""`-u`/`-p`, `HOSTCTL_PASSWORD`, the URI's own password, and the prompt.

The prompt happens only on a terminal and only when a host asks for a
password; with no terminal a host that needs one fails at once (126).
"""

from __future__ import annotations

import io
import time
import types

import pytest

pytest.importorskip("duho")

from duho import mcp as duho_mcp  # noqa: E402

from hostctl.cli import main  # noqa: E402
from hostctl.cli._common import PasswordPrompt, no_password  # noqa: E402
from hostctl.cli._root import Hostctl  # noqa: E402


class _TTY(io.StringIO):
    def isatty(self):
        return True


class _Pipe(io.StringIO):
    def isatty(self):
        return False


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("HOSTCTL_PASSWORD", raising=False)
    monkeypatch.delenv("DUHO_TRACEBACK", raising=False)
    monkeypatch.setattr("sys.stdin", _Pipe())


@pytest.fixture
def prompts(monkeypatch):
    asked = []

    def getpass(prompt="Password: "):
        asked.append(prompt)
        return "typed"

    monkeypatch.setattr("getpass.getpass", getpass)
    return asked


@pytest.fixture
def opened(monkeypatch):
    """Record what each host is opened with; nothing connects."""
    calls = []

    class _Host:
        def __init__(self, uri, **credentials):
            calls.append((uri, credentials))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def info(self):
            return types.SimpleNamespace()

    monkeypatch.setattr("hostctl.cli._common.Host", _Host)
    return calls


def _password(opened):
    return opened[-1][1].get("password")


# -- sources and their order --------------------------------------------------


def test_the_flag_beats_the_environment(opened, monkeypatch):
    monkeypatch.setenv("HOSTCTL_PASSWORD", "from-env")
    assert main(["info", "-p", "from-flag", "ssh://h"]) == 0
    assert _password(opened) == "from-flag"


def test_the_environment_is_used_without_the_flag(opened, monkeypatch):
    monkeypatch.setenv("HOSTCTL_PASSWORD", "from-env")
    assert main(["info", "ssh://h"]) == 0
    assert _password(opened) == "from-env"


def test_an_exported_password_is_never_printed_in_help_or_a_tool_schema(
    capsys, monkeypatch
):
    monkeypatch.setenv("HOSTCTL_PASSWORD", "from-env")
    with pytest.raises(SystemExit):
        main(["info", "--help"])
    shown = capsys.readouterr().out + str(duho_mcp.describe_tools(Hostctl))
    assert "from-env" not in shown
    assert "HOSTCTL_PASSWORD" in shown


def test_an_empty_exported_password_is_no_password_and_never_prompts(
    opened, prompts, monkeypatch
):
    monkeypatch.setattr("sys.stdin", _TTY())
    monkeypatch.setenv("HOSTCTL_PASSWORD", "")
    assert main(["info", "ssh://u:in-uri@h"]) == 0
    assert _password(opened) == ""
    assert prompts == []


def test_the_flag_and_the_environment_override_the_uris_password(opened, monkeypatch):
    assert main(["info", "--password", "from-flag", "ssh://u:in-uri@h"]) == 0
    assert _password(opened) == "from-flag"
    monkeypatch.setenv("HOSTCTL_PASSWORD", "from-env")
    assert main(["info", "ssh://u:in-uri@h"]) == 0
    assert _password(opened) == "from-env"


def test_the_uris_own_password_is_left_to_the_uri(opened, prompts, monkeypatch):
    monkeypatch.setattr("sys.stdin", _TTY())
    assert main(["info", "ssh://u:in-uri@h"]) == 0
    assert "password" not in opened[-1][1]
    assert prompts == []


def test_an_empty_flag_is_no_password_and_never_prompts(opened, prompts, monkeypatch):
    monkeypatch.setattr("sys.stdin", _TTY())
    assert main(["info", "-p", "", "ssh://u:in-uri@h"]) == 0
    assert _password(opened) == ""
    assert prompts == []


def test_on_a_terminal_the_prompt_waits_until_a_host_asks(opened, prompts, monkeypatch):
    monkeypatch.setattr("sys.stdin", _TTY())
    assert main(["info", "ssh://h"]) == 0
    ask = _password(opened)
    assert isinstance(ask, PasswordPrompt)
    assert prompts == []
    assert ask() == "typed" and ask() == "typed"
    assert prompts == ["Password: "]


def test_without_a_terminal_a_host_that_asks_gets_an_error(opened, prompts):
    assert main(["info", "ssh://h"]) == 0
    assert _password(opened) is no_password
    with pytest.raises(PermissionError, match="-p PASSWORD or set HOSTCTL_PASSWORD"):
        no_password()
    assert prompts == []


def test_a_scheme_without_passwords_is_offered_nothing(opened, monkeypatch):
    monkeypatch.setattr("sys.stdin", _TTY())
    monkeypatch.setenv("HOSTCTL_PASSWORD", "from-env")
    assert main(["info", "qemu+libvirt:///vm"]) == 0
    assert opened[-1][1] == {}
    monkeypatch.delenv("HOSTCTL_PASSWORD")
    assert main(["info", "docker://web"]) == 0
    assert opened[-1][1] == {}


def test_an_explicit_password_a_scheme_cannot_take_is_refused(opened, capsys):
    assert main(["info", "-p", "x", "docker://web"]) == 125
    assert capsys.readouterr().err == "hostctl: docker: takes no password (-p)\n"
    assert opened == []


# -- -u ----------------------------------------------------------------------


def test_username_is_passed_and_overrides_the_uris(opened):
    assert main(["info", "-u", "bob", "ssh://alice@h"]) == 0
    assert opened[-1] == ("ssh://alice@h", {"username": "bob", "password": no_password})


def test_username_reaches_the_config(monkeypatch):
    from hostctl.host._config import HostConfig

    seen = []
    monkeypatch.setattr(
        "hostctl.cli._common.Host",
        lambda uri, **credentials: seen.append(HostConfig(uri, **credentials))
        or (_ for _ in ()).throw(ConnectionError("stop")),
    )
    assert main(["info", "--username", "bob", "-p", "pw", "ssh://alice@h"]) == 125
    assert seen[0].username == "bob" and seen[0].password == "pw"


def test_username_on_a_scheme_without_one_is_refused(opened, capsys):
    assert main(["info", "-u", "bob", "docker://web"]) == 125
    assert capsys.readouterr().err == "hostctl: docker: takes no username (-u)\n"


def test_fan_out_shares_one_prompt(opened, prompts, monkeypatch):
    monkeypatch.setattr("sys.stdin", _TTY())
    assert main(["info", "-u", "bob", "ssh://a,b,c"]) == 0
    asks = {id(credentials["password"]) for _, credentials in opened}
    assert len(opened) == 3 and len(asks) == 1
    assert all(credentials["username"] == "bob" for _, credentials in opened)


# -- MCP ---------------------------------------------------------------------


def test_a_tool_call_takes_username_and_password(opened):
    result = duho_mcp.call_tool(
        Hostctl,
        "hostctl.info",
        {"uri": "ssh://alice@h", "username": "bob", "password": "pw"},
    )
    assert not result.get("isError"), result
    assert opened[-1][1] == {"username": "bob", "password": "pw"}


def test_a_tool_call_never_prompts(opened, prompts, monkeypatch):
    monkeypatch.setattr("sys.stdin", _TTY())
    result = duho_mcp.call_tool(Hostctl, "hostctl.info", {"uri": "ssh://h"})
    assert not result.get("isError"), result
    assert _password(opened) is no_password
    assert prompts == []


# -- a real SSH server -------------------------------------------------------


@pytest.fixture
def ssh(monkeypatch):
    """An in-process server; the CLI's SSH config gets its key and known_hosts."""
    pytest.importorskip("asyncssh")
    from conformance.inprocess_ssh import InProcessSSHServer
    from hostctl.transports.ssh._config import SSHConfig

    async def handler(process):
        process.stdout.write(b"ok\n")
        process.exit(0)

    servers = []
    original = SSHConfig.connect_opts

    def connect_opts(self):
        opts = original(self)
        server = servers[-1]
        opts.update(
            client_keys=[server.client_key_path],
            known_hosts=server.known_hosts_path,
            agent_path=None,
        )
        return opts

    monkeypatch.setattr(SSHConfig, "connect_opts", connect_opts)

    def start(**options):
        server = InProcessSSHServer(handler, **options)
        servers.append(server)
        return server

    return start


def _uri(server, user="tester"):
    return f"ssh://{user}@127.0.0.1:{server.port}"


def test_without_a_terminal_a_password_login_fails_fast_with_126(ssh, prompts, capsys):
    with ssh(accept_keys=False, password="pw") as server:
        started = time.monotonic()
        code = main(["run", _uri(server), "--", "true"])
        elapsed = time.monotonic() - started
    err = capsys.readouterr().err
    assert code == 126
    assert elapsed < 15
    assert len(err.splitlines()) == 1 and "HOSTCTL_PASSWORD" in err and "-p" in err
    assert prompts == []


def test_on_a_terminal_the_password_is_asked_once(ssh, prompts, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": prompts.append(1) or "pw")
    monkeypatch.setattr("sys.stdin", _TTY())
    with ssh(accept_keys=False, password="pw") as server:
        assert main(["run", _uri(server), "--", "true"]) == 0
    assert prompts == [1]


def test_a_key_login_never_prompts(ssh, monkeypatch):
    monkeypatch.setattr("sys.stdin", _TTY())
    monkeypatch.setattr(
        "getpass.getpass", lambda prompt="": pytest.fail("prompted for a password")
    )
    with ssh(password="pw") as server:
        assert main(["run", _uri(server), "--", "true"]) == 0


def test_the_flag_logs_in_and_an_empty_one_is_refused(
    ssh, prompts, monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdin", _TTY())
    with ssh(accept_keys=False, password="pw") as server:
        assert main(["run", "-p", "pw", _uri(server), "--", "true"]) == 0
        assert main(["run", "-p", "", _uri(server), "--", "true"]) == 126
    assert prompts == []
    assert "permission denied" in capsys.readouterr().err


def test_username_replaces_the_uris_user_on_the_wire(ssh):
    with ssh() as server:
        assert main(["run", "-u", "tester", _uri(server, "nobody"), "--", "true"]) == 0
    users = {event[1] for event in server.events if event[0] == "begin_auth"}
    assert users == {"tester"}


# -- --ssh-config ------------------------------------------------------------

_ALIAS_URI = "ssh://tester@hostctl-alias.invalid"


def _alias_config(server) -> str:
    return (
        "Host hostctl-alias.invalid\n"
        "    HostName 127.0.0.1\n"
        f"    Port {server.port}\n"
    )


def _authenticated(server) -> bool:
    return any(event[0] == "begin_auth" for event in server.events)


def test_without_the_flag_the_users_ssh_config_is_read(ssh, fake_home):
    with ssh() as server:
        (fake_home / ".ssh" / "config").write_text(_alias_config(server))
        assert main(["run", _ALIAS_URI, "--", "true"]) == 0
    assert _authenticated(server)


def test_an_empty_flag_reads_no_ssh_config(ssh, fake_home, capsys):
    with ssh() as server:
        (fake_home / ".ssh" / "config").write_text(_alias_config(server))
        assert main(["run", "--ssh-config", "", _ALIAS_URI, "--", "true"]) != 0
    assert not _authenticated(server)


def test_the_flag_names_a_file_even_without_a_slash(
    ssh, fake_home, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    with ssh() as server:
        (tmp_path / "ssh_config").write_text(_alias_config(server))
        assert (
            main(["run", "--ssh-config", "ssh_config", _ALIAS_URI, "--", "true"]) == 0
        )
    assert _authenticated(server)


def test_the_flag_on_a_scheme_without_an_ssh_config_is_refused(capsys):
    assert main(["info", "--ssh-config", "cfg", "local:"]) == 125
    assert capsys.readouterr().err == (
        "hostctl: local: takes no ssh config (--ssh-config)\n"
    )


def test_a_tool_call_takes_ssh_config_as_a_path(ssh, fake_home, tmp_path):
    tools = {tool["name"]: tool for tool in duho_mcp.describe_tools(Hostctl)}
    assert "ssh_config" in tools["hostctl.run"]["inputSchema"]["properties"]
    with ssh() as server:
        path = tmp_path / "ssh_config"
        path.write_text(_alias_config(server))
        result = duho_mcp.call_tool(
            Hostctl,
            "hostctl.run",
            {"uri": _ALIAS_URI, "ssh_config": str(path), "--": ["true"]},
        )
    assert not result.get("isError"), result
    assert _authenticated(server)


# -- WinRM -------------------------------------------------------------------


def test_winrm_without_a_terminal_fails_before_connecting(monkeypatch, capsys):
    winrm = pytest.importorskip("winrm")
    monkeypatch.setattr(
        winrm, "Session", lambda *a, **k: pytest.fail("connected without a password")
    )
    started = time.monotonic()
    code = main(["run", "winrm://bob@win.example?provider=pywinrm", "--", "x"])
    assert code == 126 and time.monotonic() - started < 15
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and "HOSTCTL_PASSWORD" in err


def test_winrm_on_a_terminal_asks_before_connecting(monkeypatch, prompts):
    winrm = pytest.importorskip("winrm")
    seen = []

    def session(endpoint, auth, **options):
        seen.append(auth)
        raise ConnectionError("stop here")

    monkeypatch.setattr(winrm, "Session", session)
    monkeypatch.setattr("sys.stdin", _TTY())
    assert main(["run", "winrm://bob@win.example?provider=pywinrm", "--", "x"]) == 125
    # The transport may build its session again after the failure; the
    # answer is reused, never asked twice.
    assert seen and set(seen) == {("bob", "typed")}
    assert prompts == ["Password: "]
