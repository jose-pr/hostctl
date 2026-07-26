"""SSH provider command construction without a live endpoint."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest
from pathlib_next import Path as NextPath, PosixPathname, WindowsPathname

from hostctl import (
    BASH,
    CMD,
    POWERSHELL,
    PWSH,
    HostConfig,
    Shell,
    SshConfig,
)
from hostctl.shell import PowerShellFlavour
from hostctl.host._ssh import _SshTransport


class _Result:
    def __init__(self, returncode=0, stdout=b"stub-out", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _StubSSH:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or _Result()
        self.closed = False
        self.waited = False

    def is_closed(self):
        return False

    async def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self.result

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited = True


class _TimeoutSSH(_StubSSH):
    async def run(self, command, **kwargs):
        raise TimeoutError


def _host(shell=None, result=None):
    host = _SshTransport(
        shell or SshConfig("nas.example.com", username="admin", password="secret")
    )
    stub = _StubSSH(result)
    host._ssh = stub
    return host, stub


def test_ssh_result_is_real_completed_process_and_asyncssh_never_checks():
    host, stub = _host()
    result = host.run("echo hello")
    assert type(result) is subprocess.CompletedProcess
    assert result.stdout == b"stub-out"
    assert stub.calls[0][1]["check"] is False


def test_ssh_text_mode_selects_an_encoding_for_executor():
    host, stub = _host(result=_Result(stdout="text"))
    result = host.run("echo hello", text=True)
    assert result.stdout == "text"
    assert stub.calls[0][1]["encoding"] == "utf-8"


def test_ssh_shell_execute_path_quotes_it_as_a_direct_command():
    host, stub = _host()
    Shell(host.shell_flavour, host.executor).execute(PurePosixPath("/opt/my command"))
    assert stub.calls[0][0] == "/opt/my command"


def test_ssh_shell_execute_path_with_args_renders_safe_script():
    host, stub = _host()
    Shell(host.shell_flavour, host.executor).execute(
        PurePosixPath("/opt/my command"),
        "--name",
        "value with spaces",
    )
    assert "/opt/my command" in stub.calls[0][0]
    assert "value with spaces" in stub.calls[0][0]


def test_ssh_forwards_stderr_merge_sentinel():
    host, stub = _host()
    host.run("echo hello", stderr=subprocess.STDOUT)
    assert stub.calls[0][1]["stderr"] is subprocess.STDOUT


def test_ssh_inherited_capture_stream_without_fileno(monkeypatch):
    host, stub = _host()
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    host.run("echo hello", capture_output=False)
    assert stub.calls[0][1]["stdout"] is None
    assert output.getvalue() == "stub-out"


def test_ssh_host_context_closes_connection_on_exception():
    host, stub = _host()
    with pytest.raises(RuntimeError):
        host.connect()
        try:
            raise RuntimeError("body failed")
        finally:
            host.close()
    assert stub.closed
    assert stub.waited


def test_ssh_config_uses_explicit_authentication_fields():
    config = SshConfig(
        "host",
        username="admin",
        password="secret",
        client_keys="keydata",
        known_hosts=None,
    )
    assert config.connect_opts() == {
        "username": "admin",
        "password": "secret",
        "client_keys": ["keydata"],
        "known_hosts": None,
    }


def test_ssh_key_path_is_not_reencoded():
    key = Path("id_ed25519")
    assert SshConfig("host", client_keys=key).connect_opts()["client_keys"] == [key]


def test_ssh_check_is_applied_after_completed_process_conversion():
    host, _ = _host(result=_Result(returncode=7, stderr=b"bad"))
    with pytest.raises(subprocess.CalledProcessError) as raised:
        host.run("exit 7")
    assert raised.value.returncode == 7
    assert raised.value.stderr == b"bad"


def test_ssh_timeout_uses_subprocess_exception_contract():
    host = _SshTransport(SshConfig("host"))
    host._ssh = _TimeoutSSH()

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        host.run("sleep", timeout=0.25)

    assert raised.value.cmd.endswith("sleep")
    assert raised.value.timeout == 0.25


def test_posix_dialect_quotes_cwd_and_embeds_environment():
    host, stub = _host()
    host.run(["printf", "%s", "a b"], cwd="/tmp/a b", env={"NAME": "value"})
    command, options = stub.calls[0]
    assert "cd -- '/tmp/a b'" in command
    assert command.count("cd -- ") == 1
    assert "'a b'" in command
    assert "export NAME=value" in command
    assert options["env"] is None


def test_ssh_partial_capture_does_not_pass_local_streams_to_asyncssh(
    monkeypatch,
):
    host, stub = _host(result=_Result(stdout="out", stderr="err"))
    inherited = io.StringIO()
    monkeypatch.setattr(sys, "stderr", inherited)

    result = host.run("command", capture_output="stdout", text=True)

    assert result.stdout == "out"
    assert result.stderr is None
    assert inherited.getvalue() == "err"
    assert stub.calls[0][1]["stdout"] is None
    assert stub.calls[0][1]["stderr"] is None


def test_ssh_output_stream_remains_owned_by_caller():
    host, _ = _host(result=_Result(stdout=b"out", stderr=b"err"))
    stdout = io.BytesIO()
    stderr = io.BytesIO()

    result = host.run(
        "command",
        stdout=stdout,
        stderr=stderr,
        capture_output=False,
    )

    assert result.stdout is None
    assert result.stderr is None
    assert stdout.getvalue() == b"out"
    assert stderr.getvalue() == b"err"


def test_ssh_input_stream_remains_owned_by_caller():
    host, stub = _host()
    stdin = io.BytesIO(b"input")

    host.run("command", stdin=stdin)

    assert not stdin.closed
    forwarded = stub.calls[0][1]["stdin"]
    assert forwarded is not stdin
    assert forwarded.read() == b"input"


def test_powershell_dialect_uses_powershell_quoting_cwd_and_environment():
    shell = SshConfig("windows.example.com", dialect="powershell")
    host, stub = _host(shell)
    host.run(
        ["Write-Output", "a'b"],
        cwd=r"C:\Program Files",
        env={"NAME": "a'b"},
    )
    command, options = stub.calls[0]
    assert "powershell.exe" in command
    assert "Set-Location -LiteralPath 'C:\\Program Files'" in command
    assert "$env:NAME='a''b'" in command
    assert "'Write-Output' 'a''b'" in command
    assert options["env"] is None


def test_shell_validates_dialect_and_path_style():
    with pytest.raises(ValueError):
        SshConfig("host", dialect="csh")
    with pytest.raises(TypeError):
        SshConfig("host", path_flavor="native")


def test_ssh_string_options_normalize_to_shell_flavours():
    config = SshConfig("host", dialect="powershell", path_flavor=WindowsPathname)
    assert config.dialect is POWERSHELL
    assert isinstance(config.dialect, PowerShellFlavour)
    assert config.path_flavor is WindowsPathname
    assert str(config.dialect) == "powershell"


def test_ssh_auto_selection_round_trips_and_detects_posix_shell():
    config = SshConfig("host", dialect="auto")
    restored = HostConfig(config.connection_uri)
    host, stub = _host(config, result=_Result(stdout="/bin/bash\n", stderr=""))

    assert restored.dialect == "auto"
    assert host.shell_flavour is BASH
    assert host.shell_flavour is BASH
    assert len(stub.calls) == 1


def test_ssh_auto_rejects_executable_override():
    with pytest.raises(ValueError, match="executable"):
        SshConfig("host", dialect="auto", executable="/bin/custom")


@pytest.mark.parametrize(
    ("responses", "expected"),
    (
        (((0, "HOSTCTL_PWSH_7\n"),), PWSH),
        (((1, ""), (0, "HOSTCTL_POWERSHELL_5\n")), POWERSHELL),
        (((1, ""), (1, ""), (0, "HOSTCTL_CMD\n")), CMD),
    ),
)
def test_ssh_auto_detects_windows_shell_in_probe_order(responses, expected):
    class _ProbeSSH(_StubSSH):
        async def run(self, command, **kwargs):
            self.calls.append((command, kwargs))
            returncode, stdout = responses[len(self.calls) - 1]
            return _Result(returncode=returncode, stdout=stdout, stderr="")

    host = _SshTransport(SshConfig("host", dialect="auto", path_flavor=PureWindowsPath))
    stub = _ProbeSSH()
    host._ssh = stub

    assert host.shell_flavour is expected
    assert len(stub.calls) == list((PWSH, POWERSHELL, CMD)).index(expected) + 1


def test_ssh_path_flavor_requires_a_concrete_pure_path_constructor():
    assert SshConfig("host").path_flavor is PosixPathname
    config = SshConfig("host", path_flavor=PureWindowsPath)
    assert config.path_flavor is PureWindowsPath
    with pytest.raises(TypeError, match="pure-path class"):
        SshConfig("host", path_flavor="windows")
    with pytest.raises(TypeError, match="local OS"):
        SshConfig("host", path_flavor=PurePath)


def test_sftp_path_styles_when_ssh_extra_is_available():
    pytest.importorskip("asyncssh")
    posix = _SshTransport(SshConfig("host")).path("/etc")
    windows = _SshTransport(SshConfig("host", path_flavor=WindowsPathname)).path(
        r"C:\Temp"
    )
    assert str(posix) == "sftp://host:22/etc"
    assert str(windows) == "sftp://host:22/C:/Temp"
    assert isinstance(posix, NextPath)
    assert isinstance(windows, NextPath)


def test_sftp_backend_is_reused_and_invalidated_on_close():
    host, stub = _host()
    first = host.path("/etc")
    second = host.path("/var")
    assert first.backend is second.backend
    backend = first.backend
    host.close()
    assert host._sftp_backend is None
    assert stub.closed and stub.waited
    # A later path call gets a fresh backend after lifecycle close.
    assert host.path("/tmp").backend is not backend


def test_auto_posix_probe_preserves_shell_executable():
    host, stub = _host(
        SshConfig("host", dialect="auto"),
        result=_Result(stdout="/usr/local/bin/bash\n", stderr=""),
    )
    host.run("echo hello")
    command, _ = stub.calls[-1]
    assert "/usr/local/bin/bash" in command
