"""Transport-independent shell construction and executor binding."""

import os
import subprocess
import sys
import typing
from collections import deque
from pathlib import PurePosixPath

import pytest

from hostctl import (
    BASH,
    CMD,
    FISH,
    Executor,
    ExecutorCapability,
    ExecutorCommand,
    ExecutionOptions,
    LocalConfig,
    POSIX_SHELL,
    POWERSHELL,
    PWSH,
    Shell,
    ShellOperator,
    ShellSession,
    SshConfig,
    WinRMConfig,
    ZSH,
    register_shell_flavour,
    shell_flavour,
)
from hostctl.host._ssh import _SshTransport
from hostctl.host._winrm import _WinRMTransport
from hostctl.executor.psrp import PsrpExecutor
from hostctl.executor import Executor as ModuleExecutor
from hostctl.executor import SshExecutor, WinRMExecutor


def test_shell_callable_executor_receives_one_script():
    commands = []
    shell = Shell(POWERSHELL, commands.append)

    result = shell.run(
        ["Write-Output", "first"],
        ["Write-Output", "second"],
        cwd=r"C:\Temp",
        env={"NAME": "value"},
    )

    assert result is None
    assert len(commands) == 1
    assert "Write-Output" in commands[0]
    assert "Set-Location -LiteralPath 'C:\\Temp'" in commands[0]
    assert "$env:NAME='value'" in commands[0]


def test_executor_contracts_are_direct_hostctl_api():
    assert Executor is ModuleExecutor
    assert Executor in Shell.__mro__
    assert Executor in SshExecutor.__mro__
    assert Executor in WinRMExecutor.__mro__


def test_executor_contract_declares_subprocess_basics():
    hints = typing.get_type_hints(Executor.__call__)
    assert {
        "stdin",
        "stdout",
        "stderr",
        "cwd",
        "env",
        "capture_output",
        "check",
        "encoding",
        "errors",
        "input",
        "timeout",
        "text",
    } <= hints.keys()


def test_shell_accepts_host_shaped_executor():
    class _Host:
        def __init__(self):
            self.commands = []

        def run(self, command):
            self.commands.append(command)
            return 7

    host = _Host()
    shell = Shell(POSIX_SHELL, host)

    assert shell.run("echo hello", env={"NAME": "a b"}) == 7
    assert host.commands == ["export NAME='a b';echo hello"]


def test_shell_execute_preserves_path_for_executor():
    commands = []
    shell = Shell(POSIX_SHELL, commands.append)

    shell.execute(PurePosixPath("/usr/local/bin/tool"))

    assert commands == [PurePosixPath("/usr/local/bin/tool")]


def test_shell_passes_context_separately_when_executor_accepts_it():
    calls = []

    def executor(command, *, cwd=None, env=None):
        calls.append((command, cwd, env))
        return "done"

    shell = Shell(POSIX_SHELL, executor)
    result = shell.run(
        "echo hello",
        cwd=PurePosixPath("/tmp/work"),
        env={"NAME": "value"},
    )

    assert result == "done"
    assert calls == [
        (
            "echo hello",
            PurePosixPath("/tmp/work"),
            {"NAME": "value"},
        )
    ]


def test_shell_execute_rejects_context_the_executor_cannot_accept():
    shell = Shell(POSIX_SHELL, lambda command: command)

    try:
        shell.execute("echo hello", cwd="/tmp")
    except TypeError as exc:
        assert str(exc) == "executor does not accept cwd"
    else:
        raise AssertionError("unsupported execution context was silently ignored")


def test_shell_forwards_executor_io_and_text_options_unchanged():
    calls = []

    def executor(command, *, stdin=None, stdout=None, encoding=None, text=False):
        calls.append((command, stdin, stdout, encoding, text))
        return "result"

    shell = Shell(POSIX_SHELL, executor)
    result = shell.run(
        "echo hello",
        stdin="input-stream",
        stdout="output-stream",
        encoding="utf-8",
        text=True,
    )

    assert result == "result"
    assert calls == [
        (
            "echo hello",
            "input-stream",
            "output-stream",
            "utf-8",
            True,
        )
    ]


def test_executor_can_distinguish_command_from_script_by_value_type():
    calls = []

    def executor(command):
        calls.append(command)

    shell = Shell(POSIX_SHELL, executor)
    shell.execute(PurePosixPath("/usr/bin/true"))
    shell.execute("printf direct-shell-text")
    shell.run(("printf", "%s", "hello"))

    assert calls == [
        PurePosixPath("/usr/bin/true"),
        "printf direct-shell-text",
        "printf %s hello",
    ]


def test_shell_passes_native_args_only_when_executor_supports_them():
    class _NativeExecutor:
        executor_capabilities = frozenset((ExecutorCapability.ARGS,))

        def __init__(self):
            self.calls = []

        def __call__(self, command, *args):
            self.calls.append((command, args))

    executor = _NativeExecutor()
    Shell(POSIX_SHELL, executor).execute(
        PurePosixPath("/usr/bin/tool"),
        "--name",
        "value with spaces",
    )

    assert executor.calls == [
        (
            PurePosixPath("/usr/bin/tool"),
            ("--name", "value with spaces"),
        )
    ]


def test_shell_renders_args_when_executor_has_no_native_args():
    calls = []
    Shell(POSIX_SHELL, calls.append).execute(
        PurePosixPath("/usr/bin/tool"),
        "--name",
        "value with spaces",
    )

    assert calls == ["/usr/bin/tool --name 'value with spaces'"]


def test_posix_raw_commands_preserve_shell_operators():
    script = POSIX_SHELL.script(
        (
            "printf first && printf second",
            "printf third | cat > output.txt",
        )
    )

    assert script == (
        "printf first && printf second;" "printf third | cat > output.txt"
    )


def test_shell_flavour_environment_script_is_reusable():
    assert (
        POSIX_SHELL.environment_script({"NAME": "value with spaces", b"BYTES": b"data"})
        == "export NAME='value with spaces';export BYTES=data"
    )


@pytest.mark.parametrize("flavour", [POSIX_SHELL, FISH, POWERSHELL, CMD])
def test_shell_flavours_reject_control_characters(flavour):
    with pytest.raises(ValueError, match="control"):
        flavour.quote("bad\x00value")


def test_shell_normalizes_bytes_deques_and_empty_commands():
    assert POSIX_SHELL.script((b"echo hi",)) == "'echo hi'"
    assert POSIX_SHELL.script((deque(("printf", "%s", "ok")),)) == "printf %s ok"
    assert POSIX_SHELL.script(("", "echo ok")) == "echo ok"
    with pytest.raises(ValueError, match="must not be empty"):
        POSIX_SHELL.script(((),))


def test_powershell_doubles_all_tokenizer_smart_quotes():
    assert POWERSHELL.quote("it's ‘smart’‚safe‛") == "'it''s ‘‘smart’’‚‚safe‛‛'"


def test_shell_command_paths_use_target_path_syntax():
    from pathlib import PurePosixPath, PureWindowsPath

    assert POSIX_SHELL.command_path("/bin/sh") == PurePosixPath("/bin/sh")
    assert FISH.command_path("/usr/bin/fish") == PurePosixPath("/usr/bin/fish")
    assert POWERSHELL.command_path(r"C:\Tools\pwsh.exe") == PureWindowsPath(
        r"C:\Tools\pwsh.exe"
    )
    assert CMD.command_path(r"C:\Tools\cmd.exe") == PureWindowsPath(r"C:\Tools\cmd.exe")


def test_shell_environment_invalid_bytes_key_is_value_error():
    with pytest.raises(ValueError, match="invalid environment"):
        POSIX_SHELL.environment_script({b"\xff": "value"})


def test_powershell_execution_has_native_exit_status_epilogue():
    calls = []
    Shell(POWERSHELL, calls.append).run("Write-Output ok")
    assert calls[-1].endswith("; exit $LASTEXITCODE")


def test_cmd_disables_delayed_expansion_and_batch_percent_doubling():
    assert "/v:off" in CMD.command(("echo ok",)).command
    assert "%%" not in CMD.environment_script({"VALUE": "%PATH%"})
    assert "^%" in CMD.environment_script({"VALUE": "%PATH%"})
    assert (
        POWERSHELL.environment_script({"NAME": "value with spaces", b"BYTES": b"data"})
        == "$env:NAME='value with spaces';$env:BYTES='data'"
    )
    assert (
        POSIX_SHELL.environment_script({"PORT": 8080, "RATIO": 1.5, "ENABLED": True})
        == "export PORT=8080;export RATIO=1.5;export ENABLED=True"
    )


def test_posix_structured_commands_quote_operators_as_arguments():
    script = POSIX_SHELL.script(
        (
            ("printf", "%s", "left && right"),
            ("printf", "%s", b"bytes | data"),
        )
    )

    assert script == ("printf %s 'left && right';" "printf %s 'bytes | data'")


def test_posix_multiple_path_commands_are_quoted_individually():
    script = POSIX_SHELL.script(
        (
            PurePosixPath("/opt/first command"),
            PurePosixPath("/opt/second command"),
        )
    )

    assert script == "'/opt/first command';'/opt/second command'"


def test_explicit_shell_operators_compose_structured_commands():
    script = POSIX_SHELL.script(
        (
            ("printf", "%s", "first"),
            ShellOperator.AND,
            ("printf", "%s", "second"),
            ShellOperator.PIPE,
            ("cat",),
            ShellOperator.REDIRECT,
            PurePosixPath("/tmp/output file"),
        )
    )

    assert script == ("printf %s first&&printf %s second|cat>" "'/tmp/output file'")


def test_shell_operators_must_be_infix():
    with pytest.raises(ValueError, match="between commands"):
        POSIX_SHELL.script((ShellOperator.AND, ("true",)))
    with pytest.raises(ValueError, match="followed by"):
        POSIX_SHELL.script((("true",), ShellOperator.AND))


def test_windows_powershell_rejects_nonportable_and_operator():
    with pytest.raises(NotImplementedError, match="not portable"):
        POWERSHELL.script(
            (
                ("Write-Output", "first"),
                ShellOperator.AND,
                ("Write-Output", "second"),
            )
        )


def test_powershell_7_supports_pipeline_chain_operators():
    assert (
        PWSH.script(
            (("Write-Output", "first"), ShellOperator.AND, ("Write-Output", "second"))
        )
        == "& 'Write-Output' 'first' && & 'Write-Output' 'second'; exit $LASTEXITCODE"
    )


def test_common_shell_executables_are_explicit():
    assert POSIX_SHELL.default_executable == "/bin/sh"
    assert BASH.default_executable == "/bin/bash"
    assert ZSH.default_executable == "/bin/zsh"
    assert FISH.default_executable == "/usr/bin/fish"
    assert CMD.default_executable == "cmd.exe"
    assert POWERSHELL.default_executable == "powershell.exe"
    assert POWERSHELL.major_version == 5
    assert PWSH.default_executable == "pwsh"
    assert PWSH.major_version == 7


def test_cmd_shell_renders_environment_cwd_args_and_operators():
    script = CMD.script(
        (
            ("tool.exe", "value & data"),
            ShellOperator.AND,
            ("echo", "done"),
        ),
        cwd=r"C:\Program Files",
        env={"NAME": "100%"},
    )
    assert script == (
        'set "NAME=100^%"&cd /d C:\\Program Files&&'
        'tool.exe ^"value ^& data^"&&echo done'
    )


@pytest.mark.skipif(os.name != "nt", reason="requires cmd.exe")
def test_cmd_shell_operators_execute_on_windows():
    command = CMD.command(
        (
            ("cmd", "/c", "echo A"),
            ShellOperator.AND,
            ("cmd", "/c", "echo B"),
        )
    )

    result = subprocess.run(
        command.command,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["A", "B"]


@pytest.mark.skipif(os.name != "nt", reason="requires cmd.exe")
@pytest.mark.parametrize(
    "value",
    ("a b", "%PATH%", "a & b", 'quote"value', "bang!", "snowman \N{SNOWMAN}"),
)
def test_cmd_external_structured_arguments_round_trip_on_windows(value):
    code = "import sys; print(ascii(sys.argv[1]))"
    command = CMD.command(((sys.executable, "-c", code, value),))

    result = subprocess.run(
        command.command,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ascii(value)


@pytest.mark.skipif(os.name != "nt", reason="requires cmd.exe")
def test_cmd_builtin_arguments_do_not_leak_escaping():
    command = CMD.command((("echo", "%PATH% & bang!"),))

    result = subprocess.run(
        command.command,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "%PATH% & bang!"


def test_fish_uses_native_environment_and_boolean_syntax():
    script = FISH.script(
        (("echo", "first"), ShellOperator.AND, ("echo", "second")),
        env={"NAME": "a b"},
    )
    assert script == "set -gx NAME 'a b';echo first; and echo second"


def test_host_builds_shell_from_its_flavour_and_executor():
    ssh = _SshTransport(SshConfig("host", dialect="powershell"))
    winrm = _WinRMTransport(WinRMConfig("host", "user"))

    assert Shell(ssh.shell_flavour, ssh.executor).flavour is POWERSHELL
    assert Shell(winrm.shell_flavour, winrm.executor).flavour is POWERSHELL
    assert isinstance(ssh.executor, SshExecutor)
    assert isinstance(winrm.executor, (WinRMExecutor, PsrpExecutor))


def test_shell_session_uses_flavour_wrapper_and_provider_spawn():
    class _Process:
        returncode = None

        def __init__(self):
            self.written = ""

        def write(self, value):
            self.written += value

    class _Provider:
        executor_capabilities = frozenset()

        def __init__(self):
            self.calls = []

        def run(self, command, **options):
            raise AssertionError("session must not call run")

        def spawn(self, *commands, **options):
            self.calls.append((commands, options))
            return _Process()

    provider = _Provider()
    shell = Shell(POWERSHELL, provider)

    result = shell.session(
        ["Write-Output", "a b"],
        cwd=r"C:\Program Files",
        env={"NUMBER": 7},
        encoding="utf-8",
    )

    assert result.process is not None
    assert provider.calls[0][0] == ()
    assert "Set-Location -LiteralPath 'C:\\Program Files'" in result.process.written
    assert "$env:NUMBER='7'" in result.process.written
    assert "'Write-Output' 'a b'" in result.process.written
    assert provider.calls[0][1]["encoding"] == "utf-8"

    result.send(["Write-Output", "c d"], ShellOperator.PIPE, "Out-String")
    assert "'Write-Output' 'c d' | Out-String;\n" in result.process.written


def test_shell_session_without_spawn_fails_explicitly():
    shell = Shell(POSIX_SHELL, lambda command: None)
    with pytest.raises(NotImplementedError, match="persistent sessions"):
        shell.session("sh")


class _LifecycleProcess:
    """A process recording the context-manager calls it receives."""

    returncode = None

    def __init__(self):
        self.written = ""
        self.entered = False
        self.exited = False
        self.closed = False

    def write(self, value):
        self.written += value

    def close(self):
        self.closed = True

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.exited = True
        self.close()
        return False


class _LifecycleProvider:
    executor_capabilities = frozenset()

    def __init__(self):
        self.processes = []

    def __call__(self, command, *args, **options):
        raise AssertionError("session must not call the executor")

    def spawn(self, *commands, **options):
        process = _LifecycleProcess()
        self.processes.append(process)
        return process


def test_shell_is_a_context_manager_opening_and_closing_one_session():
    provider = _LifecycleProvider()
    shell = Shell(POSIX_SHELL, provider)

    with shell as session:
        assert isinstance(session, ShellSession)
        session.send("echo hi")

    process = provider.processes[0]
    assert process.entered is True
    assert process.exited is True
    assert process.closed is True
    assert process.written == "echo hi;\n"


def test_shell_context_manager_closes_the_session_when_the_body_raises():
    provider = _LifecycleProvider()
    shell = Shell(POSIX_SHELL, provider)

    with pytest.raises(ValueError, match="boom"):
        with shell:
            raise ValueError("boom")

    assert provider.processes[0].closed is True


def test_shell_context_manager_is_reusable_but_not_reentrant():
    provider = _LifecycleProvider()
    shell = Shell(POSIX_SHELL, provider)

    with shell:
        with pytest.raises(RuntimeError, match="active session"):
            with shell:
                pass

    # The failed re-entry must not have left the shell unusable.
    with shell:
        pass
    assert len(provider.processes) == 2


def test_shell_context_manager_without_spawn_fails_explicitly():
    shell = Shell(POSIX_SHELL, lambda command: None)
    with pytest.raises(NotImplementedError, match="persistent sessions"):
        with shell:
            pass


def _recording_shell(**defaults):
    """A shell over a capability-less executor recording rendered scripts."""
    scripts = []

    def execute(command, *args, **options):
        scripts.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    return Shell(POSIX_SHELL, execute, **defaults), scripts


def test_shell_defaults_apply_to_run_when_the_call_omits_them():
    shell, scripts = _recording_shell(cwd="/srv/app", env={"TZ": "UTC"})

    shell.run("pwd")

    assert "cd -- /srv/app&&" in scripts[0]
    assert "export TZ=UTC" in scripts[0]


def test_shell_default_cwd_is_overridden_by_the_call():
    shell, scripts = _recording_shell(cwd="/srv/app")

    shell.run("pwd", cwd="/tmp")

    assert "cd -- /tmp&&" in scripts[0]
    assert "/srv/app" not in scripts[0]


def test_shell_default_env_merges_with_the_call_env_per_key():
    shell, scripts = _recording_shell(env={"TZ": "UTC", "LANG": "C"})

    shell.run("printenv", env={"TZ": "EST"})

    # The per-call key wins; the default-only key survives.
    assert "export TZ=EST" in scripts[0]
    assert "export LANG=C" in scripts[0]
    assert "TZ=UTC" not in scripts[0]


def test_shell_env_can_opt_out_of_the_shell_defaults():
    shell, scripts = _recording_shell(cwd="/srv/app", env={"TZ": "UTC"})

    # `None` declines the shell's defaults. It does not request an empty
    # environment -- nothing is sent, so the host's own environment stands.
    shell.run("pwd", env=None)

    # The shell's environment is dropped...
    assert "export TZ" not in scripts[0]
    # ...but clearing env says nothing about cwd, which still applies.
    assert "cd -- /srv/app&&" in scripts[0]


def test_shell_empty_env_mapping_merges_nothing_and_inherits_the_default():
    shell, scripts = _recording_shell(env={"TZ": "UTC"})

    shell.run("pwd", env={})

    assert "export TZ=UTC" in scripts[0]


def test_shell_session_env_can_opt_out_of_the_shell_defaults():
    provider = _LifecycleProvider()
    shell = Shell(POSIX_SHELL, provider, cwd="/srv/app", env={"TZ": "UTC"})

    shell.session(env=None)

    written = provider.processes[0].written
    assert "export TZ" not in written
    assert "cd -- /srv/app" in written


def test_shell_configure_env_none_drops_the_inherited_default():
    shell, _ = _recording_shell(cwd="/srv/app", env={"TZ": "UTC"})

    derived = shell.configure(env=None)

    assert derived.env is None
    assert derived.cwd == "/srv/app"
    assert shell.env == {"TZ": "UTC"}


def test_shell_configure_layers_defaults_without_mutating_the_original():
    shell, _ = _recording_shell(cwd="/srv/app", env={"TZ": "UTC"})

    derived = shell.configure(env={"EXTRA": "1"}, cwd="/opt")

    assert derived.cwd == "/opt"
    assert derived.env == {"TZ": "UTC", "EXTRA": "1"}
    assert shell.cwd == "/srv/app"
    assert shell.env == {"TZ": "UTC"}


def test_shell_defaults_reach_the_opened_session():
    provider = _LifecycleProvider()
    shell = Shell(POSIX_SHELL, provider, cwd="/srv/app", env={"TZ": "UTC"})

    with shell as session:
        session.send("pwd")

    # The session submits the defaults once, as their own line: there is no
    # payload command to guard with `&&`, and the directory/environment must
    # persist for every later `send` in that shell.
    written = provider.processes[0].written
    assert "cd -- /srv/app" in written
    assert "export TZ=UTC" in written
    assert written.startswith("export TZ=UTC;cd -- /srv/app;")


def test_host_shell_call_returns_a_configured_shell_and_bare_access_does_not():
    host = LocalConfig()._create_host()
    host.connect()
    try:
        configured = host.shell(cwd="/srv/app", env={"TZ": "UTC"})
        assert configured.cwd == "/srv/app"
        assert configured.env == {"TZ": "UTC"}
        # Bare access stays default-free, and is still usable as a Shell.
        assert host.shell.cwd is None
        assert host.shell.env is None
        assert host.shell.run("echo hi", capture_output=True).returncode == 0
    finally:
        host.close()


def test_shell_flavour_accepts_class_and_registered_string():
    class _Custom(type(POSIX_SHELL)):
        name = "test-custom-shell"

    constructed = shell_flavour(_Custom)
    assert isinstance(constructed, _Custom)
    registered = register_shell_flavour(constructed)
    assert shell_flavour("test-custom-shell") is registered


def test_host_bound_shell_detects_native_cwd_and_env_support():
    """A host-bound Shell must see through the provider's string vocabulary.

    `ExecutorProvider` flattens capability enums to their string values, so
    `Shell` comparing `ExecutorCapability.CWD in ...` against that set was
    always False: the double-render guard never fired and a host with native
    cwd/env still got a rendered `cd`/`export` prefix in the script.
    """
    from hostctl import ExecutorProvider, PosixHost

    seen = []

    def execute(command, *args, **options):
        seen.append((command, args, options))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    host = PosixHost(
        executor_providers=(
            ExecutorProvider("native", execute, capabilities=("args", "cwd", "env")),
        ),
        shell=POSIX_SHELL,
    )
    shell = host.shell

    # The vocabularies now agree, so the guard is live.
    assert shell.executor_capabilities == frozenset(("args", "cwd", "env"))
    assert shell._executor_accepts_cwd is True
    assert shell._executor_accepts_env is True

    shell.run(["echo", "hi"], cwd="/tmp/target", env={"K": "V"})
    command, args, options = seen[-1]

    # Native context is forwarded rather than swallowed...
    assert options["cwd"] == "/tmp/target"
    assert options["env"] == {"K": "V"}
    # ...and therefore must not also be rendered into the script text.
    rendered = " ".join([str(command), *(str(item) for item in args)])
    assert "cd " not in rendered
    assert "K=V" not in rendered


def test_shell_capability_vocabulary_is_strings_and_keeps_provider_tokens():
    """Enum members and strings normalize to one string vocabulary.

    Provider-specific tokens with no `ExecutorCapability` member must survive
    the normalization -- flattening toward the enum instead would drop them.
    """

    class _EnumExecutor:
        executor_capabilities = frozenset(
            (ExecutorCapability.ARGS, ExecutorCapability.CWD)
        )

        def __call__(self, command, *args, **options):
            return subprocess.CompletedProcess(command, 0, b"", b"")

    class _StringExecutor:
        executor_capabilities = frozenset(("args", "cwd", "runspace", "manages_status"))

        def __call__(self, command, *args, **options):
            return subprocess.CompletedProcess(command, 0, b"", b"")

    enum_shell = Shell(POSIX_SHELL, _EnumExecutor())
    string_shell = Shell(POSIX_SHELL, _StringExecutor())

    assert enum_shell.executor_capabilities == frozenset(("args", "cwd"))
    assert all(isinstance(item, str) for item in string_shell.executor_capabilities)
    # Non-enum provider tokens are preserved verbatim.
    assert {"runspace", "manages_status"} <= string_shell.executor_capabilities
    # Both vocabularies reach the same conclusion about native cwd.
    assert enum_shell._executor_accepts_cwd is True
    assert string_shell._executor_accepts_cwd is True
    assert enum_shell._executor_accepts_env is False
