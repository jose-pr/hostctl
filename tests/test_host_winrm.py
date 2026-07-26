"""WinRM provider behavior with an injected fake pywinrm session."""

from __future__ import annotations

import subprocess
import sys
import io
from pathlib import PureWindowsPath

import pytest

from pathlib_next import Path as NextPath
from hostctl import Shell, WinRMConfig, WinRMPath
from hostctl.host._winrm import _WinRMTransport
from hostctl.executor.winrm import NativeWinRMSession


class _Response:
    def __init__(self, status_code=0, out=b"ok", err=b""):
        self.status_code = status_code
        self.std_out = out
        self.std_err = err


class _Session:
    def __init__(self, response=None):
        self.response = response or _Response()
        self.scripts = []
        self.closed = False

    def run_ps(self, script):
        self.scripts.append(script)
        return self.response

    def close(self):
        self.closed = True


def _host(response=None):
    host = _WinRMTransport(
        WinRMConfig("windows.example.com", "admin", "secret", provider="pywinrm")
    )
    session = _Session(response)
    host._session = session
    return host, session


def test_winrm_success_returns_completed_process_and_decodes():
    host, _ = _host(_Response(out="café".encode()))
    result = host.run("Write-Output café", encoding="utf-8")
    assert type(result) is subprocess.CompletedProcess
    assert result.stdout == "café"


def test_winrm_text_mode_decodes_with_utf8_default():
    host, _ = _host(_Response(out="café".encode()))
    assert host.run("Write-Output café", text=True).stdout == "café"


def test_winrm_shell_execute_path_invokes_it_as_a_command():
    host, session = _host()
    Shell(host.shell_flavour, host.executor).execute(
        PureWindowsPath(r"C:\Program Files\tool.exe")
    )
    assert session.scripts == [r"C:\Program Files\tool.exe"]


def test_winrm_context_closes_when_session_supports_close():
    host, session = _host()
    host.connect()
    try:
        pass
    finally:
        host.close()
    assert session.closed
    assert host._session is None


def test_winrm_failure_check_and_no_check():
    host, _ = _host(_Response(status_code=5, err=b"bad"))
    with pytest.raises(subprocess.CalledProcessError):
        host.run("throw 'bad'")
    assert host.run("throw 'bad'", check=False).returncode == 5


def test_winrm_structured_command_cwd_and_env_are_powershell_safe():
    host, session = _host()
    host.run(
        ["Write-Output", "a'b"],
        cwd=r"C:\Program Files",
        env={"NAME": "a'b"},
    )
    script = session.scripts[0]
    assert script.startswith(
        "Set-Location -LiteralPath 'C:\\Program Files' -ErrorAction Stop;"
    )
    assert "$env:NAME='a''b'" in script
    assert "& 'Write-Output' 'a''b'" in script


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 1},
        {"executable": "cmd.exe"},
        {"input": b"x"},
    ],
)
def test_winrm_rejects_unsupported_subprocess_features(kwargs):
    host, _ = _host()
    with pytest.raises(NotImplementedError):
        host.run("hostname", **kwargs)


def test_winrm_accepts_bufsize_and_caller_owned_output_streams():
    host, _ = _host(_Response(out=b"out", err=b"err"))
    stdout = io.BytesIO()
    stderr = io.BytesIO()

    result = host.run(
        "hostname",
        bufsize=0,
        stdout=stdout,
        stderr=stderr,
        capture_output=False,
    )

    assert result.stdout is None
    assert result.stderr is None
    assert stdout.getvalue() == b"out"
    assert stderr.getvalue() == b"err"
    assert not stdout.closed
    assert not stderr.closed


def test_winrm_validates_environment_keys_and_builds_windows_path():
    host, _ = _host()
    with pytest.raises(ValueError):
        host.run("hostname", env={"BAD;Remove-Item": "x"})
    path = host.path("C:", "Temp", "a b.txt")
    assert isinstance(path, NextPath)
    assert isinstance(path, WinRMPath)
    assert str(path) == r"C:\Temp\a b.txt"
    assert path.parent.backend is path.backend
    assert str(host.path("C:")) == "C:\\"


def test_winrm_missing_dependency_is_lazy(monkeypatch):
    host = _WinRMTransport(WinRMConfig("host", "user", "password", provider="pywinrm"))
    monkeypatch.setitem(sys.modules, "winrm", None)
    with pytest.raises(ImportError, match=r"hostctl\[winrm\]"):
        _ = host.session


def test_winrm_config_exposes_secure_transport_settings():
    config = WinRMConfig(
        "host",
        "user",
        "password",
        ssl=True,
        server_cert_validation="validate",
        message_encryption="always",
        operation_timeout_sec=40,
        read_timeout_sec=50,
    )
    assert config.endpoint == "https://host:5986/wsman"
    assert config.server_cert_validation == "validate"
    assert config.message_encryption == "always"


def test_winrm_path_budget_tracks_provider(monkeypatch):
    monkeypatch.setattr("hostctl.host._winrm.pypsrp_available", lambda: False)
    pywinrm = _WinRMTransport(WinRMConfig("host", "user", "secret", provider="pywinrm"))
    assert pywinrm._path_backend.max_script_bytes == 6000
    monkeypatch.setattr("hostctl.host._winrm.pypsrp_available", lambda: True)
    psrp = _WinRMTransport(WinRMConfig("host", "user", "secret", provider="psrp"))
    assert psrp._path_backend.max_script_bytes == 256000


def test_native_winrm_timeout_names_remote_host(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(kwargs.get("input", "remote"), 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        NativeWinRMSession("server.example", timeout=1).run_ps("Write-Output x")
    assert exc.value.cmd == "server.example"


def test_native_winrm_remote_marker_is_checkable(monkeypatch):
    marker = b"HOSTCTL_NATIVE_ERROR:RemoteError:" + __import__("base64").b64encode(
        b"remote failed"
    )

    class Result:
        returncode = 5
        stdout = b""
        stderr = marker

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Result())
    host = _WinRMTransport(
        WinRMConfig("server.example", "user", "secret", provider="pywinrm")
    )
    host._session = NativeWinRMSession("server.example")
    result = host.run("Write-Output x", check=False)
    assert result.returncode == 5
    assert result.stderr == b"remote failed"


def test_native_winrm_rejects_unrepresentable_message_encryption():
    with pytest.raises(NotImplementedError, match="message encryption"):
        NativeWinRMSession("server.example", message_encryption="always")


def test_native_winrm_option_assembly(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["input"] = kwargs["input"]
        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    NativeWinRMSession(
        "server.example",
        ssl=True,
        port=5987,
        server_cert_validation="ignore",
    ).run_ps("Write-Output x")
    wrapper = captured["input"].decode("utf-8")
    assert "UseSSL=$true" in wrapper
    assert "Port=5987" in wrapper
    assert "SkipCACheck=$true" in wrapper


@pytest.mark.parametrize(
    "kwargs",
    [
        {"transport": "guess"},
        {"transport": "certificate"},
        {"server_cert_validation": "maybe"},
        {"message_encryption": "sometimes"},
        {"port": 70000},
    ],
)
def test_winrm_config_rejects_invalid_transport_settings(kwargs):
    with pytest.raises(ValueError):
        WinRMConfig("host", "user", "password", **kwargs)
