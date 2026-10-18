"""A callable `password=` is asked only when a password is needed."""

from __future__ import annotations

import pytest

from hostctl.transports.winrm import WinRMConfig


class _Asked:
    def __init__(self, answer="pw"):
        self.answer = answer
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.answer


# -- SSH: asyncssh calls it when the server asks for a password ---------------


@pytest.fixture
def ssh_server():
    pytest.importorskip("asyncssh")
    from conformance.inprocess_ssh import InProcessSSHServer

    async def handler(process):
        process.stdout.write(b"ok\n")
        process.exit(0)

    def make(**options):
        return InProcessSSHServer(handler, **options)

    return make


#: Keeps a local ssh-agent out of it: the server decides which method works.
_NO_AGENT = {"agent_path": None}


def _run(config):
    with config.open() as host:
        return host.run("true", check=False)


def test_ssh_never_asks_when_the_key_is_accepted(ssh_server):
    asked = _Asked()
    with ssh_server(password="pw") as server:
        assert (
            _run(server.config(connect_options=_NO_AGENT, password=asked)).returncode
            == 0
        )
        assert ("password", "tester") not in server.events
    assert asked.calls == 0


def test_ssh_asks_when_the_server_wants_a_password(ssh_server):
    asked = _Asked()
    with ssh_server(accept_keys=False, password="pw") as server:
        assert (
            _run(server.config(connect_options=_NO_AGENT, password=asked)).returncode
            == 0
        )
        assert ("password", "tester") in server.events
    assert asked.calls == 1


def test_an_error_from_the_callable_is_the_root_of_the_refusal(ssh_server):
    def refuse():
        raise PermissionError("no terminal")

    with ssh_server(accept_keys=False, password="pw") as server:
        with pytest.raises(ConnectionError) as raised:
            _run(server.config(connect_options=_NO_AGENT, password=refuse))
    cause = raised.value
    while getattr(cause, "cause", None) is not None:
        cause = cause.cause
    assert isinstance(cause, PermissionError) and str(cause) == "no terminal"


# -- WinRM: the leg calls it as it builds its session ------------------------


@pytest.fixture
def winrm_module(monkeypatch):
    import hostctl.transports.winrm._transport as module

    monkeypatch.setattr(module, "has_psrp", lambda: False)
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("USERDOMAIN", "CORP")
    return module


@pytest.mark.parametrize("user", ("alice", "CORP\\alice", "alice@CORP"))
def test_winrm_auto_takes_native_for_the_current_user_without_asking(
    winrm_module, monkeypatch, user
):
    monkeypatch.setattr(winrm_module.os, "name", "nt")
    asked = _Asked()

    config = WinRMConfig("server", username=user, password=asked)

    assert winrm_module._select_leg(config) == "native"
    assert asked.calls == 0


@pytest.mark.parametrize(
    "os_name, user, auth",
    (
        ("nt", "bob", "ntlm"),
        ("posix", "alice", "ntlm"),
        ("nt", "alice", "credssp"),
    ),
)
def test_winrm_auto_asks_when_native_cannot_serve(
    winrm_module, monkeypatch, os_name, user, auth
):
    monkeypatch.setattr(winrm_module.os, "name", os_name)
    asked = _Asked()
    config = WinRMConfig("server", username=user, password=asked, auth=auth, tls=True)

    assert winrm_module._select_leg(config) == "pywinrm"
    assert asked.calls == 0
    assert winrm_module._resolve_password(config) == "pw"
    assert asked.calls == 1


def test_the_pywinrm_leg_asks_as_it_builds_its_session(winrm_module, monkeypatch):
    pytest.importorskip("winrm")
    import winrm

    seen = []

    class _Session:
        def __init__(self, endpoint, auth, **options):
            seen.append(auth)
            self.protocol = type("P", (), {"transport": type("T", (), {})()})()

    monkeypatch.setattr(winrm, "Session", _Session)
    asked = _Asked("secret")
    transport = winrm_module._WinRMTransport(
        WinRMConfig("server", username="bob", password=asked, provider="pywinrm")
    )
    assert asked.calls == 0

    transport._leg._build()

    assert seen == [("bob", "secret")] and asked.calls == 1


def test_winrm_none_from_the_callable_is_a_permission_error():
    from hostctl.transports.winrm._config import _resolve_password

    config = WinRMConfig(
        "server", username="bob", password=lambda: None, provider="pywinrm"
    )
    with pytest.raises(PermissionError, match="needs a password"):
        _resolve_password(config)


def test_kerberos_never_asks():
    from hostctl.transports.winrm._config import _resolve_password

    asked = _Asked()
    config = WinRMConfig(
        "server", username="bob", password=asked, auth="kerberos", provider="pywinrm"
    )
    assert _resolve_password(config) is None and asked.calls == 0


def test_native_accepts_a_callable_and_refuses_a_string():
    assert WinRMConfig("server", username="bob", password=_Asked(), provider="native")
    with pytest.raises(ValueError, match="current Windows logon"):
        WinRMConfig("server", username="bob", password="pw", provider="native")
