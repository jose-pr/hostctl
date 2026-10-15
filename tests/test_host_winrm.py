"""WinRM provider behavior with an injected fake pywinrm session."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import io
from pathlib import PureWindowsPath

import pytest

from pathapi import Path as NextPath
from hostctl import Shell, WinRMConfig, WinRMPath
from hostctl.host._winrm import _SCRIPT_BUDGET, _WinRMTransport
from hostctl.executor.winrm import NativeWinRMSession, _PywinrmStdinSession


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
    host._leg._session = session
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
    assert host._leg._session is None


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
        _ = host._leg.session


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


@pytest.mark.parametrize("provider", ["pywinrm", "psrp"])
def test_every_leg_uses_one_script_budget(monkeypatch, provider):
    monkeypatch.setattr("hostctl.host._winrm.pypsrp_available", lambda: True)
    transport = _WinRMTransport(
        WinRMConfig("host", "user", "secret", provider=provider)
    )
    assert transport._path_backend.max_script_bytes == _SCRIPT_BUDGET


class _RecordingProtocol:
    def __init__(self, fail=False):
        self.calls = []
        self.sends = []
        self.fail = fail

    def open_shell(self):
        self.calls.append("open_shell")
        return "S"

    def run_command(self, shell, command, arguments=(), **flags):
        self.calls.append(("run_command", shell, command, list(arguments), flags))
        return "C"

    def send_command_input(self, shell, command_id, data, end=False):
        assert isinstance(data, bytes)
        self.sends.append((len(data), end))

    def get_command_output(self, shell, command_id):
        if self.fail:
            raise RuntimeError("boom")
        return b"out", b"err", 3

    def cleanup_command(self, shell, command_id):
        self.calls.append("cleanup_command")

    def close_shell(self, shell):
        self.calls.append("close_shell")


def test_the_stdin_session_starts_powershell_without_cmd_and_streams_the_script():
    protocol = _RecordingProtocol()
    response = _PywinrmStdinSession(protocol).run_ps("x" * 200_000)

    assert (response.status_code, response.std_out, response.std_err) == (
        3,
        b"out",
        b"err",
    )
    assert protocol.calls[1] == (
        "run_command",
        "S",
        "powershell.exe",
        ["-NoProfile", "-NonInteractive", "-Command", "-"],
        {"console_mode_stdin": False, "skip_cmd_shell": True},
    )
    assert len(protocol.sends) == 5
    assert all(size <= 65_536 for size, _ in protocol.sends)
    assert [end for _, end in protocol.sends] == [False] * 4 + [True]
    assert protocol.calls[-2:] == ["cleanup_command", "close_shell"]


def test_the_stdin_session_sends_one_slice_for_an_empty_script():
    protocol = _RecordingProtocol()
    _PywinrmStdinSession(protocol).run_ps("")
    assert len(protocol.sends) == 1 and protocol.sends[0][1] is True


def test_the_stdin_session_closes_the_shell_when_the_read_fails():
    protocol = _RecordingProtocol(fail=True)
    with pytest.raises(RuntimeError, match="boom"):
        _PywinrmStdinSession(protocol).run_ps("Write-Output x")
    assert protocol.calls[-2:] == ["cleanup_command", "close_shell"]


def test_native_winrm_timeout_names_remote_host(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(kwargs.get("input", "remote"), 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        NativeWinRMSession("server.example", timeout=1).run_ps("Write-Output x")
    assert exc.value.cmd == "server.example"


def _native_result(stderr=b"", stdout=b"", returncode=0):
    class Result:
        pass

    result = Result()
    result.returncode, result.stdout, result.stderr = returncode, stdout, stderr
    return result


def test_a_forged_error_line_in_remote_stderr_is_only_stderr(monkeypatch):
    """The error line is trusted only with this call's own marker, on the
    last stderr line; a remote command can print anything else."""
    forged = b"HOSTCTL_NATIVE_ERROR:RemoteError:" + base64.b64encode(b"nope")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _native_result(stderr=forged, returncode=5),
    )
    from hostctl.executor.winrm import WinRMExecutor

    result = WinRMExecutor(lambda: NativeWinRMSession("server.example"))(
        "Write-Output x", check=False
    )
    assert result.returncode == 5
    assert result.stderr == forged


def test_a_line_with_another_calls_marker_is_only_stderr(monkeypatch):
    from hostctl.executor.winrm import WinRMExecutor, exit_marker

    other = f"{exit_marker()}:error:auth:{base64.b64encode(b'x').decode()}".encode()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _native_result(stderr=other + b"\n", returncode=1),
    )
    executor = WinRMExecutor(lambda: NativeWinRMSession("server.example"))
    assert executor("Write-Output x", check=False).returncode == 1


@pytest.mark.parametrize(
    ("kind", "error"), [("auth", PermissionError), ("transport", ConnectionError)]
)
def test_a_marked_error_line_raises_by_kind(monkeypatch, kind, error):
    from hostctl.executor.winrm import WinRMExecutor

    import hostctl.executor.winrm as module

    monkeypatch.setattr(module, "exit_marker", lambda: "MARK_TEST")
    session = NativeWinRMSession("server.example")

    def fake_run(argv, **kwargs):
        line = f"MARK_TEST:error:{kind}:{base64.b64encode(b'why').decode()}\n"
        return _native_result(stderr=line.encode(), returncode=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(error, match="why"):
        WinRMExecutor(lambda: session)("Write-Output x", check=False)


def test_native_run_ps_timeout_is_per_call(monkeypatch):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(kwargs["timeout"])
        return _native_result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    session = NativeWinRMSession("server.example", timeout=30.0)
    session.run_ps("Write-Output x")
    session.run_ps("Write-Output x", timeout=2)
    assert seen == [30.0, 2]


def test_only_the_native_leg_accepts_a_per_call_timeout(monkeypatch):
    seen = []

    class _Timed:
        def run_ps(self, script, timeout=None):
            seen.append(timeout)
            return _Response()

    from hostctl.executor.winrm import WinRMExecutor

    with pytest.raises(NotImplementedError):
        WinRMExecutor(lambda: _Timed())("hostname", timeout=3)
    WinRMExecutor(lambda: _Timed(), accepts_timeout=True)("hostname", timeout=3)
    assert seen == [3]


def test_manages_status_is_an_instance_capability():
    from hostctl.executor.winrm import WinRMExecutor

    plain = WinRMExecutor(lambda: None)
    managed = WinRMExecutor(lambda: None, manages_status=True)
    assert "manages_status" not in plain.executor_capabilities
    assert "manages_status" in managed.executor_capabilities


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
    from hostctl.executor.winrm import exit_marker, status_epilogue

    marker = exit_marker()
    wrapper = NativeWinRMSession("server.example")._wrapper("cmd /c exit 7", marker)
    # The epilogue runs inside the remote script block, so the code travels
    # back as output; `Invoke-Command` never copies the remote $LASTEXITCODE.
    epilogue = base64.b64encode(status_epilogue(marker).encode("utf-8")).decode("ascii")
    assert epilogue in wrapper
    assert "$b=[ScriptBlock]::Create($s+[Environment]::NewLine+$e);" in wrapper
    # The marker line is consumed locally rather than reaching the caller.
    assert "StartsWith($m)" in wrapper
    assert wrapper.endswith("exit 1}")
    assert "$out+=" not in wrapper


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
    local = _in_a_job(wrapper)
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
        input=_in_a_job(echoing).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == b"a"


def _in_a_job(wrapper):
    """Replace the remote hop with a job: a separate PowerShell process, so a
    scope of its own where `exit` ends the block and error records are relayed
    the way `Invoke-Command` relays them."""
    needle = "Invoke-Command @o -ScriptBlock $b"
    assert needle in wrapper
    return wrapper.replace(
        needle, "Start-Job -ScriptBlock $b | Receive-Job -Wait -AutoRemoveJob"
    )


_WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="the wrapper is a Windows PowerShell program"
)


def _run_wrapper(wrapper, hop):
    return subprocess.run(
        ("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "-"),
        input=hop(wrapper).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _through_a_job(monkeypatch):
    real = subprocess.run

    def hop_run(argv, **kwargs):
        kwargs["input"] = _in_a_job(kwargs["input"].decode("utf-8")).encode("utf-8")
        return real(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", hop_run)


@_WINDOWS_ONLY
@pytest.mark.parametrize(
    ("command", "status", "output"),
    [
        ("Write-Output ok", 0, "ok"),
        ("cmd /c exit 7", 7, ""),
        (r"Get-Item C:\definitely\missing", 1, ""),
        ("throw 'boom'", 1, ""),
        ("Write-Error 'soft failure'; Write-Output 'after'", 0, "after"),
        ('cmd /c "echo to-stderr 1>&2 & exit 3"', 3, ""),
        (
            r"Write-Output before; Get-Content C:\definitely\missing.txt",
            1,
            "before",
        ),
    ],
)
def test_native_remote_errors_are_statuses_not_exceptions(
    monkeypatch, command, status, output
):
    """A remote error used to raise ConnectionError, so `check=False` could
    not inspect it and `Get-Item missing` was an outage."""
    from hostctl.executor.winrm import WinRMExecutor
    from hostctl.shell import POWERSHELL

    _through_a_job(monkeypatch)
    executor = WinRMExecutor(
        lambda: NativeWinRMSession("server.example", timeout=120),
        manages_status=True,
    )
    result = executor(
        POWERSHELL.script((command,), for_session=True),
        check=False,
        encoding="utf-8",
    )
    assert result.returncode == status
    assert result.stdout.strip() == output
    if status and "exit 7" not in command:
        assert result.stderr.strip()


@_WINDOWS_ONLY
def test_native_relay_returns_every_line_in_order(monkeypatch):
    from hostctl.executor.winrm import WinRMExecutor

    _through_a_job(monkeypatch)
    executor = WinRMExecutor(
        lambda: NativeWinRMSession("server.example", timeout=120),
        manages_status=True,
    )
    result = executor(
        '1..5000 | ForEach-Object { "line $_" }', check=False, encoding="utf-8"
    )
    assert result.stdout.splitlines() == [f"line {n}" for n in range(1, 5001)]


@_WINDOWS_ONLY
@pytest.mark.parametrize(
    ("error_id", "error"),
    [
        ("AccessDenied,PSSessionStateBroken", PermissionError),
        ("CannotConnect,PSSessionStateBroken", ConnectionError),
    ],
)
def test_native_transport_failures_raise_by_kind(error_id, error):
    """The hop writes the error record `Invoke-Command` writes when the
    session cannot be established."""
    from hostctl.executor.winrm import WinRMExecutor

    session = NativeWinRMSession("server.example", timeout=120)
    needle = "Invoke-Command @o -ScriptBlock $b"
    original = session._wrapper

    def wrapper(script, marker=None):
        return original(script, marker).replace(
            needle,
            "Write-Error -Exception "
            "([Management.Automation.Remoting.PSRemotingTransportException]::new('no session')) "
            f"-ErrorId '{error_id}' -Category OpenError",
        )

    session._wrapper = wrapper
    with pytest.raises(error, match="no session"):
        WinRMExecutor(lambda: session)("Write-Output x", check=False)


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

    assert transport._leg.name == "native"

    # With a password there is a credential to hand PSRP, so `auto` takes it.
    credentialed = winrm_module._WinRMTransport(
        WinRMConfig("server", "admin", "secret")
    )
    assert credentialed._leg.name == "psrp"


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
    transport._leg.session
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


@pytest.mark.parametrize("transport", ["basic", "plaintext", "ssl"])
def test_basic_credentials_over_http_are_refused(transport):
    """These three send `Authorization: Basic user:password` over plain HTTP
    -- on pywinrm; pypsrp refused at construction -- so the same config sent a
    password in clear or not depending only on which extra was installed."""
    with pytest.raises(ValueError, match="cleartext"):
        WinRMConfig("host", "user", "secret", transport=transport)
    from hostctl import HostConfig

    with pytest.raises(ValueError, match="cleartext"):
        HostConfig(f"winrm://user@host?transport={transport}")


@pytest.mark.parametrize("transport", ["basic", "plaintext", "ssl"])
def test_basic_over_https_and_an_explicit_opt_in_build(transport):
    from hostctl import HostConfig

    assert WinRMConfig("host", "user", "secret", transport=transport, ssl=True)
    config = WinRMConfig(
        "host", "user", transport=transport, allow_cleartext=True, port=5985
    )
    assert "allow_cleartext=true" in config.connection_uri
    assert HostConfig(str(config)) == config
    assert "allow_cleartext" not in WinRMConfig("host", "user").connection_uri


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("TRUE", True), ("on", True), ("0", False), ("No", False)],
)
def test_allow_cleartext_query_is_a_strict_boolean(value, expected):
    from hostctl import HostConfig

    config = HostConfig(
        f"winrm://user@host?transport=basic&allow_cleartext={value}"
        if expected
        else f"winrm://user@host?allow_cleartext={value}"
    )
    assert config.allow_cleartext is expected
    with pytest.raises(ValueError, match="allow_cleartext"):
        HostConfig("winrm://user@host?allow_cleartext=maybe")


@pytest.mark.parametrize(
    ("provider", "password", "os_name", "psrp", "transport", "leg"),
    [
        ("auto", None, "nt", True, "ntlm", "native"),
        ("auto", None, "nt", False, "ntlm", "native"),
        ("auto", None, "posix", True, "kerberos", "psrp"),
        ("auto", None, "posix", False, "kerberos", "pywinrm"),
        ("auto", "secret", "nt", True, "ntlm", "psrp"),
        ("auto", "secret", "posix", False, "ntlm", "pywinrm"),
        ("native", None, "posix", True, "ntlm", "native"),
        ("pywinrm", "secret", "nt", True, "ntlm", "pywinrm"),
        ("pywinrm", None, "nt", True, "kerberos", "pywinrm"),
        ("psrp", "secret", "posix", False, "ntlm", "psrp"),
    ],
)
def test_one_rule_picks_the_leg(
    monkeypatch, provider, password, os_name, psrp, transport, leg
):
    import hostctl.host._winrm as winrm_module

    monkeypatch.setattr(winrm_module.os, "name", os_name)
    monkeypatch.setattr(winrm_module, "pypsrp_available", lambda: psrp)
    config = WinRMConfig(
        "server", "admin", password, transport=transport, provider=provider
    )

    assert winrm_module._select_leg(config) == leg
    assert winrm_module._WinRMTransport(config)._leg.name == leg


def test_auto_without_a_password_off_windows_needs_a_password(monkeypatch):
    import hostctl.host._winrm as winrm_module

    monkeypatch.setattr(winrm_module.os, "name", "posix")
    with pytest.raises(ValueError, match="pass password="):
        winrm_module._select_leg(WinRMConfig("server", "admin"))


def test_provider_native_means_no_password_and_pywinrm_psrp_need_one():
    from hostctl import HostConfig

    with pytest.raises(ValueError, match="current Windows logon"):
        WinRMConfig("server", "admin", "secret", provider="native")
    for provider in ("pywinrm", "psrp"):
        with pytest.raises(ValueError, match="password="):
            WinRMConfig("server", "admin", provider=provider)
        assert WinRMConfig("server", "admin", provider=provider, transport="kerberos")
    native = WinRMConfig("server", "admin", provider="native")
    assert HostConfig(str(native)) == native
    with pytest.raises(ValueError, match="native"):
        WinRMConfig("server", "admin", provider="bogus")


def test_a_config_without_a_port_survives_its_own_uri():
    from hostctl import HostConfig

    config = WinRMConfig("h", "u")
    assert HostConfig(str(config)) == config
    pinned = WinRMConfig("h", "u", port=5986, ssl=True)
    assert HostConfig(str(pinned)) == pinned


def test_the_psrp_leg_manages_its_own_status():
    """The transport once decided this from a second place: PSRP declared it,
    the transport did not, so the flavour's `exit` epilogue was appended and
    `cmd /c exit 7` reported 1."""
    scripts = []

    class _Runspace:
        def connect(self):
            pass

        def invoke(self, script, *, raw=False, capture_exit=False, format_text=False):
            from hostctl.process.psrp import PipelineResult, PipelineStreams

            scripts.append(script)
            return PipelineResult((), PipelineStreams(), "Completed", False, 7)

    transport = _WinRMTransport(
        WinRMConfig("server", "admin", "secret", provider="psrp")
    )
    transport._leg._runspace = _Runspace()

    assert "manages_status" in transport.capabilities
    assert "runspace" in transport.capabilities
    result = transport.run("cmd /c exit 7", check=False)

    assert result.returncode == 7
    assert "exit $(" not in scripts[0]


def test_two_threads_open_one_runspace_and_close_closes_it():
    import threading
    import time

    import hostctl.host._winrm as winrm_module

    opened, closed = [], []

    class _Pool:
        def open(self):
            opened.append(self)

        def close(self):
            closed.append(self)

    class _Slow(winrm_module.RunspaceSession):
        def __init__(self, config):
            time.sleep(0.05)  # widen the check-then-assign window
            super().__init__(config)

        def _make_pool(self):
            return _Pool()

    original = winrm_module.RunspaceSession
    winrm_module.RunspaceSession = _Slow
    try:
        transport = _WinRMTransport(WinRMConfig("h", "u", "p", provider="psrp"))
        threads = [threading.Thread(target=transport.runspace) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        transport.close()
    finally:
        winrm_module.RunspaceSession = original

    assert (len(opened), len(closed)) == (1, 1)


def test_a_non_psrp_leg_has_no_runspace():
    transport = _WinRMTransport(WinRMConfig("h", "u", "p", provider="pywinrm"))
    with pytest.raises(NotImplementedError, match="PSRP runspaces"):
        transport.runspace()
    assert "runspace" not in transport.capabilities
