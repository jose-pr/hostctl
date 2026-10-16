"""SSH provider command construction without a live endpoint."""

from __future__ import annotations

import io
import shlex
import subprocess
import sys
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest
from pathapi import Path as NextPath, PosixPathname, WindowsPathname

from hostctl.shell import BASH, CMD, POWERSHELL, PWSH
from hostctl.host._config import HostConfig
from hostctl.shell._shell import Shell
from hostctl.transports.ssh._config import SSHConfig
from hostctl.shell._powershell import PowerShellFlavour
from hostctl.transports.ssh._transport import _SSHTransport


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

    async def create_process(self, command, **kwargs):
        """asyncssh's real shape: `run()` is create_process + process.wait()."""
        self.calls.append((command, kwargs))
        self.process = _StubProcess(getattr(self, "wait_result", None) or self.result)
        return self.process

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited = True


class _StubWriter:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    async def drain(self):
        return None

    def write_eof(self):
        self.written.append(b"")


class _StubReader:
    def __init__(self, data=b""):
        self._data = data

    async def read(self, size=-1):
        value, self._data = self._data, b""
        return value


class _StubProcess:
    """The process `create_process()` hands back, as asyncssh does."""

    def __init__(self, result, error=None):
        self._result = result
        self._error = error
        self.waits = []
        self.terminated = False
        self.closed = False
        self.stdin = _StubWriter()
        self.stdout = _StubReader()
        self.stderr = _StubReader()
        self.returncode = getattr(result, "returncode", None)

    async def wait(self, check=False, timeout=None):
        self.waits.append({"check": check, "timeout": timeout})
        if self._error is not None:
            raise self._error
        return self._result

    def terminate(self):
        self.terminated = True

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class _TimeoutSSH(_StubSSH):
    def __init__(self, result=None):
        super().__init__(result)
        self.process = None

    async def create_process(self, command, **kwargs):
        self.calls.append((command, kwargs))
        self.process = _StubProcess(None, error=TimeoutError())
        return self.process


def _compose(config, stub):
    """A composed host over one transport whose connection is `stub`.

    `host.transport` is the shared transport, for assertions about it.
    """
    from hostctl import _async
    from hostctl.system._kinds import PosixHost, WindowsHost
    from hostctl.transports.ssh._providers import ssh_providers

    executor, paths = ssh_providers(config)
    transport = executor.transport
    transport._ssh = stub
    transport._loop_generation = _async.loop_generation()
    kind = WindowsHost if issubclass(config.pathname, PureWindowsPath) else PosixHost
    host = kind(
        config,
        executor_providers=(executor,),
        path_providers=(paths,),
        shell=(
            config.shell
            if config.shell != "auto"
            else (lambda: transport.shell_flavour)
        ),
    )
    host.transport = transport
    return host


def _host(shell=None, result=None):
    config = shell or SSHConfig("nas.example.com", username="admin", password="secret")
    stub = _StubSSH(result)
    return _compose(config, stub), stub


def test_ssh_result_is_real_completed_process_and_asyncssh_never_checks():
    host, stub = _host()
    result = host.run("echo hello")
    assert type(result) is subprocess.CompletedProcess
    assert result.stdout == b"stub-out"
    # hostctl applies `check` itself; asyncssh is always asked not to raise.
    assert stub.process.waits[0]["check"] is False


def test_ssh_text_mode_selects_an_encoding_for_executor():
    host, stub = _host(result=_Result(stdout="text"))
    result = host.run("echo hello", text=True)
    assert result.stdout == "text"
    assert stub.calls[0][1]["encoding"] == "utf-8"


def test_ssh_shell_run_quotes_a_path_command():
    host, stub = _host()
    Shell(host.shell_flavour, host).run(PurePosixPath("/opt/my command"))
    assert "'/opt/my command'" in stub.calls[0][0]


def test_ssh_shell_run_with_args_renders_safe_script():
    host, stub = _host()
    Shell(host.shell_flavour, host).run(
        (PurePosixPath("/opt/my command"), "--name", "value with spaces")
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
    config = SSHConfig(
        "host",
        username="admin",
        password="secret",
        client_keys="keydata",
        verify=False,
    )
    assert config.connect_opts() == {
        "username": "admin",
        "password": "secret",
        "client_keys": ["keydata"],
        "known_hosts": None,
        "config": None,
        "connect_timeout": 30.0,
    }


def test_default_config_still_verifies_the_host_key_on_every_leg():
    """`known_hosts` is never omitted, whatever its value.

    Both legs are handed the same options, so neither may rely on a library
    default: an SFTP client that seeds `known_hosts=None` for an option it is
    not given would accept any server key, including a substituted one, and
    send it the password.
    """
    options = SSHConfig("host", password="secret").connect_opts()

    assert "known_hosts" in options
    assert options["known_hosts"] == ()


def test_opting_out_of_host_key_verification_stays_explicit():
    assert SSHConfig("host", verify=False).connect_opts()["known_hosts"] is None
    with pytest.raises(ValueError, match="verify=False"):
        SSHConfig("host", known_hosts=None)


def test_ssh_key_path_is_not_reencoded():
    key = Path("id_ed25519")
    assert SSHConfig("host", client_keys=key).connect_opts()["client_keys"] == [key]


def test_ssh_check_is_applied_after_completed_process_conversion():
    host, _ = _host(result=_Result(returncode=7, stderr=b"bad"))
    with pytest.raises(subprocess.CalledProcessError) as raised:
        host.run("exit 7")
    assert raised.value.returncode == 7
    assert raised.value.stderr == b"bad"


def test_ssh_timeout_uses_subprocess_exception_contract():
    host = _compose(SSHConfig("host"), _TimeoutSSH())

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        host.run("sleep", timeout=0.25)

    assert raised.value.cmd.endswith("sleep")
    assert raised.value.timeout == 0.25


def test_posix_dialect_quotes_cwd_and_embeds_environment():
    host, stub = _host()
    host.run(["printf", "%s", "a b"], cwd="/tmp/a b", env={"NAME": "value"})
    command, options = stub.calls[0]
    # The login shell reads the line; the script is its third word.
    script = shlex.split(command)[2]
    assert "cd -- '/tmp/a b'" in script
    assert script.count("cd -- ") == 1
    assert "'a b'" in script
    assert "export NAME=value" in script
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
    shell = SSHConfig("windows.example.com", shell="powershell")
    host, stub = _host(shell)
    host.run(
        ["Write-Output", "a'b"],
        cwd=r"C:\Program Files",
        env={"NAME": "a'b"},
    )
    submitted, options = stub.calls[0]
    assert "powershell.exe" in submitted
    command = _decoded_powershell(submitted)
    assert "Set-Location -LiteralPath 'C:\\Program Files'" in command
    assert "$env:NAME='a''b'" in command
    assert "'Write-Output' 'a''b'" in command
    assert options["env"] is None


def test_shell_validates_dialect_and_path_style():
    with pytest.raises(ValueError):
        SSHConfig("host", shell="csh")
    with pytest.raises(ValueError, match="'auto', 'posix', or 'windows'"):
        SSHConfig("host", pathname="native")


def test_ssh_string_options_normalize_to_shell_flavours():
    config = SSHConfig("host", shell="powershell", pathname=WindowsPathname)
    assert config.shell is POWERSHELL
    assert isinstance(config.shell, PowerShellFlavour)
    assert config.pathname is WindowsPathname
    assert str(config.shell) == "powershell"


def test_ssh_auto_selection_round_trips_and_detects_posix_shell():
    config = SSHConfig("host", shell="auto")
    restored = HostConfig(config.connection_uri)
    host, stub = _host(config, result=_Result(stdout="/bin/bash\n", stderr=""))

    assert restored.shell == "auto"
    assert host.shell_flavour is BASH
    assert host.shell_flavour is BASH
    assert len(stub.calls) == 1


def test_ssh_auto_rejects_executable_override():
    with pytest.raises(ValueError, match="executable"):
        SSHConfig("host", shell="auto", executable="/bin/custom")


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
        async def create_process(self, command, **kwargs):
            self.calls.append((command, kwargs))
            returncode, stdout = responses[len(self.calls) - 1]
            return _StubProcess(
                _Result(returncode=returncode, stdout=stdout, stderr="")
            )

    host = _SSHTransport(SSHConfig("host", shell="auto", pathname=PureWindowsPath))
    stub = _ProbeSSH()
    host._ssh = stub

    assert host.shell_flavour is expected
    assert len(stub.calls) == list((PWSH, POWERSHELL, CMD)).index(expected) + 1


def test_ssh_pathname_requires_a_concrete_pure_path_constructor():
    assert SSHConfig("host").pathname is PosixPathname
    config = SSHConfig("host", pathname=PureWindowsPath)
    assert config.pathname is PureWindowsPath
    with pytest.raises(TypeError, match="pure-path class"):
        SSHConfig("host", pathname=42)
    with pytest.raises(TypeError, match="local OS"):
        SSHConfig("host", pathname=PurePath)


def test_ssh_pathname_takes_the_same_strings_as_the_other_configs():
    assert SSHConfig("host", pathname="windows").pathname is WindowsPathname
    assert SSHConfig("host", pathname="posix").pathname is PosixPathname
    # `auto` asks the server for its login directory when the host is created.
    assert SSHConfig("host", pathname="auto").pathname == "auto"
    with pytest.raises(ValueError, match="pathname"):
        SSHConfig("host", pathname="native")


def test_sftp_path_styles_when_ssh_extra_is_available():
    pytest.importorskip("asyncssh")
    posix = _SSHTransport(SSHConfig("host")).path("/etc")
    windows = _SSHTransport(SSHConfig("host", pathname=WindowsPathname)).path(
        r"C:\Temp"
    )
    # The text is the path on the server; a Windows drive keeps its colon.
    assert str(posix) == "/etc"
    assert str(windows) == "/C:/Temp"
    assert isinstance(posix, NextPath)
    assert isinstance(windows, NextPath)


@pytest.mark.parametrize(
    "remote",
    [
        "/tmp/report%20final.txt",
        "/tmp/cache?v=2",
        "/tmp/a#b",
        "/tmp/sp ace",
        "/tmp/100%",
        "/tmp/café.txt",
    ],
)
def test_sftp_path_survives_uri_syntax_in_a_filename(remote):
    """A name that would be syntax in a URI -- `?`, `#`, `%xx` -- reaches the
    server as written: the remote path is never parsed as a URI."""
    pytest.importorskip("asyncssh")
    path = _SSHTransport(SSHConfig("host")).path(remote)
    assert str(path) == remote
    assert path.name == remote.rsplit("/", 1)[1]


def test_sftp_backend_is_reused_and_invalidated_on_close():
    host, stub = _host()
    transport = host.transport
    first = transport.path("/etc")
    second = transport.path("/var")
    assert first.backend is second.backend
    backend = first.backend
    transport.close()
    assert transport._sftp_backend is None
    assert stub.closed and stub.waited
    # A later path call gets a fresh backend after lifecycle close.
    assert transport.path("/tmp").backend is not backend


def test_auto_posix_probe_preserves_shell_executable():
    host, stub = _host(
        SSHConfig("host", shell="auto"),
        result=_Result(stdout="/usr/local/bin/bash\n", stderr=""),
    )
    host.run("echo hello")
    command, _ = stub.calls[-1]
    assert "/usr/local/bin/bash" in command


def test_a_session_starts_the_configured_shell_not_the_login_shell():
    """`spawn()` with no command resolves the program as `run()` does.

    The per-call executable alone would start the account's login shell and
    ignore SSHConfig.executable, the flavour's default and the executable
    recovered by shell="auto", while send() renders in the configured
    shell.
    """
    for config, expected in (
        (SSHConfig("h", username="root"), "/bin/sh"),
        (SSHConfig("h", username="root", executable="/bin/zsh"), "/bin/zsh"),
        (SSHConfig("h", username="root", shell="powershell"), "powershell.exe"),
    ):
        transport = _SSHTransport(config)
        stub = _StubSSH()
        transport._ssh = stub

        transport.spawn()

        assert stub.calls[-1][0] == expected


def test_a_session_write_encodes_for_a_binary_channel():
    """asyncssh does `bytearray(data)` on a str, so the documented
    `session.send("echo hi")` raised TypeError from inside a third party."""
    transport = _SSHTransport(SSHConfig("h", username="root"))
    stub = _StubSSH()
    transport._ssh = stub

    process = transport.spawn()
    process.write("echo hi" + chr(10))

    assert stub.process.stdin.written == [("echo hi" + chr(10)).encode()]


def test_wait_keeps_output_the_caller_had_not_read():
    """asyncssh's wait() clears the receive buffers into its result; only the
    return code was kept, so a later read() returned b"" -- EOF, apparently."""
    transport = _SSHTransport(SSHConfig("h", username="root"))
    stub = _StubSSH()
    stub.wait_result = _Result(returncode=0, stdout=b"late output", stderr=b"")
    transport._ssh = stub

    process = transport.spawn()
    assert process.wait() == 0
    assert process.read() == b"late output"


def _decoded_powershell(command):
    """The script inside a rendered PowerShell command.

    `PowerShellFlavour.command()` submits `-EncodedCommand <base64 utf-16le>`
    so the remote login shell -- cmd.exe on a stock Windows OpenSSH server --
    cannot reinterpret it. Assertions about what was rendered look inside.
    """
    import base64

    text = str(command)
    marker = "-EncodedCommand "
    if marker not in text:
        if " -Command " in text:
            # Delivered to a POSIX login shell: the script is the last word.
            return shlex.split(text)[-1]
        return text
    payload = text.split(marker, 1)[1].split()[0]
    return base64.b64decode(payload).decode("utf-16-le")


def test_closing_a_host_whose_sftp_invalidation_fails_still_closes_ssh():
    """The invalidation ran first and decided whether the connection was
    closed at all: a backend whose own close raised left the SSH connection
    open AND unreachable, because `_ssh` was still set and the next
    `close()` tried the same failing invalidation again."""
    host, stub = _host()

    class _FailingBackend:
        def close(self):
            raise OSError("sftp close failed")

    transport = host.transport
    transport._sftp_backend = _FailingBackend()

    with pytest.raises(OSError, match="sftp close failed"):
        transport.close()

    assert stub.closed, "the SSH connection was left open"
    assert transport._ssh is None


def test_a_forked_child_does_not_reuse_the_parents_connection(monkeypatch):
    """A connection is bound to the loop that opened it, and `fork()` leaves
    the child with the parent's objects and a dead loop thread. The child
    rebuilds the loop, but the transport kept the inherited connection and
    failed with a raw asyncio error."""
    from hostctl import _async

    host, stub = _host()
    host = host.transport
    assert host.ssh is stub

    # What the child sees: a different process, so a different loop.
    monkeypatch.setattr(_async, "loop_generation", lambda: 424242)

    opened = []

    def connect(*args, **kwargs):
        opened.append(True)

        async def make():
            return _StubSSH()

        return make()

    monkeypatch.setattr(_async, "asyncssh", lambda: type("M", (), {"connect": connect}))

    fresh = host.ssh

    assert opened == [True]
    assert fresh is not stub
    # The parent's connection is dropped, never closed: the socket is still
    # the parent's.
    assert not stub.closed


def test_the_default_verifies_the_host_key_on_both_legs():
    """The SFTP leg's library default is the opposite of the exec leg's, so
    both are pinned: `path()` must not accept a host key `run()` rejects."""
    options = SSHConfig("h").connect_opts()

    assert options == {
        "known_hosts": (),
        "config": None,
        "connect_timeout": 30.0,
    }

    # The SFTP leg gets the same dict, and reads no ssh_config, so it
    # verifies the host key and resolves the host as the exec leg does.
    transport = _SSHTransport(SSHConfig("h"))
    transport.path("/tmp/x")
    backend = transport._sftp_backend

    assert backend.connect_opts["known_hosts"] == ()
    assert backend.connect_opts["config"] is None
    assert (backend.host, backend.port, backend.user) == ("h", 22, None)


@pytest.mark.parametrize("shell", ("/bin/ash", "/bin/ksh", "/usr/bin/mksh"))
def test_ssh_auto_accepts_the_other_posix_shells(shell):
    from hostctl.shell import POSIX_SHELL

    host, _ = _host(
        SSHConfig("host", shell="auto"), result=_Result(stdout=shell + "\n")
    )

    assert host.shell_flavour is POSIX_SHELL


def test_ssh_auto_still_refuses_csh_and_says_so():
    host, _ = _host(
        SSHConfig("host", shell="auto"), result=_Result(stdout="/bin/tcsh\n")
    )

    with pytest.raises(RuntimeError, match="tcsh.*not supported"):
        host.shell_flavour


def test_a_zero_port_in_a_uri_is_refused_not_turned_into_22():
    with pytest.raises(ValueError, match="port"):
        HostConfig("ssh://h:0")
    assert HostConfig("ssh://h").port == 22
