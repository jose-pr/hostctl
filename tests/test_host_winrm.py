"""WinRM provider behavior with an injected fake pywinrm session."""

from __future__ import annotations

import base64
import os
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
    # Derived from what pywinrm actually sends: the script is base64 UTF-16-LE
    # (8/3 of its length) inside a `powershell -encodedcommand` command line
    # that cmd.exe caps at ~8180 characters. 6000 was over twice what fits.
    assert pywinrm._path_backend.max_script_bytes == 3000
    encoded = pywinrm._path_backend.max_script_bytes * 8 / 3
    assert encoded + len("powershell.exe -encodedcommand ") < 8180
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
    assert "New-PSSessionOption -SkipCACheck -SkipCNCheck" in wrapper
    assert "SessionOption=$so" in wrapper
    # `SkipCACheck` belongs to `New-PSSessionOption`; splatting it into
    # `Invoke-Command` is a parameter-binding error, which is what the old
    # string-splice produced whenever a port was configured.
    assert "SkipCACheck=$true" not in wrapper


@pytest.mark.parametrize("port", (None, 5986))
def test_native_winrm_cert_ignore_survives_both_port_spellings(port):
    wrapper = NativeWinRMSession(
        "server.example",
        ssl=True,
        port=port,
        server_cert_validation="ignore",
    )._wrapper("Write-Output x")
    # Without a port the old `str.replace` searched for ";};", which the
    # rendered wrapper never contained, so the setting vanished silently.
    assert "New-PSSessionOption -SkipCACheck -SkipCNCheck" in wrapper
    assert "SessionOption=$so" in wrapper

    validating = NativeWinRMSession("server.example", ssl=True, port=port)._wrapper(
        "Write-Output x"
    )
    assert "New-PSSessionOption" not in validating
    assert "SessionOption" not in validating


def test_native_winrm_carries_the_remote_exit_code_through_a_marker():
    from hostctl.executor.winrm import exit_marker

    marker = exit_marker()
    wrapper = NativeWinRMSession("server.example")._wrapper("cmd /c exit 7", marker)
    # The epilogue runs inside the remote script block, so the code travels
    # back as output; `Invoke-Command` never copies the remote $LASTEXITCODE.
    epilogue = base64.b64encode(
        (f"Write-Output ('{marker}:' + " "[string]([int]$LASTEXITCODE))").encode(
            "utf-8"
        )
    ).decode("ascii")
    assert epilogue in wrapper
    assert "$b=[ScriptBlock]::Create($s+[Environment]::NewLine+$e);" in wrapper
    # The marker line is consumed locally rather than reaching the caller.
    assert f"if($t.StartsWith('{marker}:'))" in wrapper
    assert "$out;exit $c}" in wrapper


def test_the_exit_marker_is_per_call_so_output_cannot_forge_it():
    """`__HOSTCTL_LASTEXITCODE__` is a string any command may print -- a log
    line, a grep over the sources -- and the wrapper removed that line from
    stdout and took the number after it as the exit status."""
    from hostctl.executor.winrm import exit_marker

    first, second = exit_marker(), exit_marker()

    assert first != second
    assert first.startswith("__HOSTCTL_LASTEXITCODE_")
    wrapper = NativeWinRMSession("server.example")._wrapper("echo x", first)
    assert second not in wrapper


@pytest.mark.skipif(
    os.name != "nt", reason="the wrapper is a Windows PowerShell program"
)
@pytest.mark.parametrize("compose", (False, True))
def test_native_winrm_wrapper_filters_the_marker_and_exits_with_it(compose):
    """Run the real wrapper with the remote hop replaced by a *runspace*.

    Not `& $b`: that runs the block in the CALLING scope, so an `exit` inside
    it ends the wrapper process directly and produces the expected status
    whether or not the marker mechanism worked at all. `Invoke-Command
    -ComputerName` and PSRP both run the block in a separate runspace, where
    `exit` ends only the block -- which is the entire thing this test exists
    to prove. It also drives the composed payload every real dispatch sends,
    not just the bare one.
    """
    from hostctl.shell import POWERSHELL

    # `for_session=True` is what a real dispatch renders for this provider:
    # the native session declares `manages_status`, so the flavour's own exit
    # epilogue is left off and the wrapper's marker epilogue is the one that
    # reports the status. Without that the payload's `exit` ends the runspace
    # before the marker is ever emitted, and the code is lost -- which is
    # exactly what this used to do.
    payload = "cmd /c exit 7"
    if compose:
        payload = POWERSHELL.script((payload,), for_session=True)
    wrapper = NativeWinRMSession("server.example")._wrapper(payload)
    local = _in_a_separate_runspace(wrapper)
    result = subprocess.run(
        ("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "-"),
        input=local.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 7, result.stderr
    assert b"__HOSTCTL_LASTEXITCODE__" not in result.stdout
    assert result.stdout == b""

    echoing = NativeWinRMSession("server.example")._wrapper(
        POWERSHELL.script((("Write-Output", "a"),), for_session=True)
        if compose
        else "Write-Output 'a'"
    )
    result = subprocess.run(
        ("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "-"),
        input=_in_a_separate_runspace(echoing).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == b"a"


def _in_a_separate_runspace(wrapper):
    """Replace the remote hop with a local runspace, which is what a remote
    one is: a scope of its own, where `exit` ends the block and not us."""
    return wrapper.replace(
        "Invoke-Command @o -ScriptBlock $b",
        "$(try{$ps=[PowerShell]::Create();"
        "[void]$ps.AddScript($b.ToString());$ps.Invoke()}"
        "finally{$ps.Dispose()})",
    )


@pytest.mark.skipif(
    os.environ.get("HOSTCTL_TEST_WINRM_NATIVE") != "1",
    reason="set HOSTCTL_TEST_WINRM_NATIVE=1 and HOSTCTL_TEST_WINRM_HOST "
    "to enable the native WinRM leg",
)
def test_native_winrm_live_remote_exit_code():
    """The one claim only a real target can settle."""
    host = os.environ["HOSTCTL_TEST_WINRM_HOST"]
    session = NativeWinRMSession(host)
    assert session.run_ps("cmd /c exit 7").status_code == 7
    assert session.run_ps("cmd /c exit 0").status_code == 0


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


@pytest.mark.skipif(os.name != "nt", reason="native context is Windows-only")
def test_native_winrm_declares_that_it_manages_status():
    """Without this the flavour appends `; exit $LASTEXITCODE`.

    That `exit` ends the remote pipeline before NativeWinRMSession's
    `__HOSTCTL_LASTEXITCODE__` marker line can be emitted, so the marker never
    comes back, the status stays 0, and `check=True` passes for a command that
    failed by exit status.
    """
    from hostctl.host._winrm import WinRMExecutorProvider

    native = _WinRMTransport(WinRMConfig("server.example", "operator"))
    assert "manages_status" in native.capabilities
    assert "manages_status" in WinRMExecutorProvider(native).capabilities


def test_pywinrm_does_not_claim_to_manage_status():
    """With explicit credentials the payload goes through pywinrm, which needs
    the flavour's epilogue to carry the status.

    Pinned on the pywinrm provider explicitly: PSRP manages status on its own,
    so with pypsrp installed "auto" would hide the distinction.
    """
    from hostctl.host._winrm import WinRMExecutorProvider

    remote = _WinRMTransport(
        WinRMConfig("server.example", "operator", password="secret", provider="pywinrm")
    )
    assert "manages_status" not in remote.capabilities
    assert "manages_status" not in WinRMExecutorProvider(remote).capabilities


def test_a_status_managing_provider_gets_a_script_without_the_epilogue():
    """The consequence the capability exists for, at the flavour boundary."""
    from hostctl.shell import POWERSHELL

    epilogue = POWERSHELL.execution_epilogue
    assert POWERSHELL.script(("cmd /c exit 7",)).endswith(epilogue)
    assert not POWERSHELL.script(("cmd /c exit 7",), for_session=True).endswith(
        epilogue
    )


def test_auto_prefers_the_native_path_a_password_free_config_documents(monkeypatch):
    """The guide says a password-free WinRM config on Windows uses native
    current-context remoting. Installing `hostctl[psrp]` silently took that
    away: PSRP won every `auto`, and PSRP needs a credential."""
    import hostctl.host._winrm as winrm_module

    monkeypatch.setattr(winrm_module, "pypsrp_available", lambda: True)
    monkeypatch.setattr(winrm_module.os, "name", "nt")

    transport = winrm_module._WinRMTransport(WinRMConfig("server", "admin"))

    assert transport._provider == "pywinrm"
    assert transport._native_session is True

    # With a password there is a credential to hand PSRP, so `auto` takes it.
    credentialed = winrm_module._WinRMTransport(
        WinRMConfig("server", "admin", "secret")
    )
    assert credentialed._provider == "psrp"


def test_the_native_session_gets_the_configured_deadline(monkeypatch):
    """`timeout=None` left the native path with no deadline at all, while
    the config validated one."""
    import hostctl.host._winrm as winrm_module

    monkeypatch.setattr(winrm_module.os, "name", "nt")
    built = {}

    class _Session:
        def __init__(self, host, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr(winrm_module, "NativeWinRMSession", _Session)

    import os as _os

    monkeypatch.setenv("USERNAME", "admin")
    monkeypatch.delenv("USERDOMAIN", raising=False)
    transport = winrm_module._WinRMTransport(
        WinRMConfig("server", "admin", read_timeout_sec=45)
    )
    transport.session
    del _os

    assert built["timeout"] == 45.0


def test_the_transports_own_run_applies_the_manages_status_rule(monkeypatch):
    """`SystemHost` leaves the flavour's exit epilogue off for a provider
    that reports its own status; this path composes its own script and used
    a different rule -- so the native wrapper's `exit` ended the remote
    pipeline before its marker line could be emitted."""
    import hostctl.host._winrm as winrm_module

    monkeypatch.setattr(winrm_module.os, "name", "nt")
    monkeypatch.setenv("USERNAME", "admin")
    monkeypatch.delenv("USERDOMAIN", raising=False)

    scripts = []

    class _Session:
        def __init__(self, host, **kwargs):
            pass

        def run_ps(self, script):
            scripts.append(script)
            return type("R", (), {"status_code": 0, "std_out": b"", "std_err": b""})()

    monkeypatch.setattr(winrm_module, "NativeWinRMSession", _Session)
    transport = winrm_module._WinRMTransport(WinRMConfig("server", "admin"))

    transport.run("cmd /c exit 7", check=False)

    assert scripts
    assert "exit $" not in scripts[0], scripts[0]
