import os
import shlex
import shutil
import subprocess
from pathlib import PurePosixPath

import pytest
from pathapi.mempath import MemPath, MemPathBackend

from hostctl.shell._grammar import Exec
from hostctl.host._config import HostConfig
from hostctl.exceptions import OperationNotStartedError
from hostctl.provider._contracts import (
    PathProvider,
    ProviderProbe,
    ExecutorProvider,
    SessionInitializer,
)
from hostctl.provider._selector import ProviderSelector
from hostctl.system._kinds import PosixHost, PosixConfig, WindowsHost
from hostctl.transports.winrm._config import WinRMConfig
from hostctl.transports.local._executor import LocalExecutor
from pathapi import Path as HostPath


class _InitProvider:
    def __init__(self, name="p", fail_close=False):
        self.name = name
        self.capabilities = frozenset()
        self.transport = self
        self.calls = []
        self.fail_close = fail_close

    def probe(self):
        return ProviderProbe("available")

    def connect(self):
        self.calls.append("connect")

    def close(self):
        self.calls.append("close")
        if self.fail_close:
            raise RuntimeError("close failed")

    def execute(self, *args, **kwargs):
        return subprocess.CompletedProcess(args, 0, b"", b"")


def test_system_uri_roundtrip_and_ordered_providers():
    config = HostConfig(
        "windows://node?executor=first&executor=second&path=rpc&path=sftp"
    )
    assert (
        str(config)
        == "windows://node?executor=first&executor=second&path=rpc&path=sftp"
    )
    assert config.executors == ("first", "second")
    assert config.paths == ("rpc", "sftp")


def test_system_initializer_default_and_generation_timeout_and_override():
    provider = _InitProvider()
    calls = []
    config_calls = []
    config = PosixConfig("node", initializer=lambda host: config_calls.append(host))
    host = PosixHost(
        config,
        executor_providers=(provider,),
        initializer=lambda connected_host: calls.append(connected_host),
    )
    host.connect()
    host.connect()
    assert calls == [host]
    assert config_calls == []
    host.close()
    host.connect()
    assert calls == [host, host]
    captured = []
    # A hook taking **kwargs still receives the timeout; one that does not
    # declare it is called without -- the documented example is
    # `SessionInitializer(lambda h: h.run("sudo -v"), timeout=10)`.
    timeout_init = SessionInitializer(
        lambda connected_host, **opts: captured.append((connected_host, opts)),
        timeout=3,
    )
    initialized = PosixHost(
        executor_providers=(_InitProvider(),), initializer=timeout_init
    )
    initialized.connect()
    assert captured == [(initialized, {"timeout": 3})]


def test_system_initializer_failure_cleans_up_without_replay():
    first, second = _InitProvider("first"), _InitProvider("second", fail_close=True)
    host = PosixHost(
        executor_providers=(first, second),
        initializer=lambda _: (_ for _ in ()).throw(ValueError("boom")),
    )
    with pytest.raises(ValueError, match="boom"):
        host.connect()
    assert first.calls.count("close") == 1
    assert second.calls.count("close") == 0
    assert host._connected is False


def test_system_config_resolves_builtin_local_descriptors():
    config = HostConfig("posix://node?executor=local&path=local")
    host = config._create_host()
    assert host.capabilities == frozenset(("run", "path"))
    assert host.path("tmp").name == "tmp"


def test_system_config_rejects_unknown_provider_descriptor():
    config = HostConfig("posix://node?executor=does-not-exist")
    with pytest.raises(ValueError, match="unknown executor provider"):
        config._create_host()


def test_system_config_resolves_transport_descriptors_with_explicit_options():
    from hostctl.transports.ssh._config import SSHConfig

    config = PosixConfig(
        "node",
        executor=("ssh",),
        path=("sftp",),
        provider_options={"ssh": SSHConfig("node", username="root")},
    )
    host = config._create_host()
    # The SSH provider implements spawn(), and now says so.
    assert host.capabilities == frozenset(("run", "path", "spawn", "tty"))


def test_provider_selector_rejects_unavailable_without_replay():
    calls = []

    def declined(*args, **kwargs):
        calls.append("declined")
        raise OperationNotStartedError("preflight")

    def selected(*args, **kwargs):
        calls.append("selected")
        return subprocess.CompletedProcess(args, 0, b"ok", b"")

    first = ExecutorProvider(
        "first", declined, probe=lambda: ProviderProbe("unavailable", "offline")
    )
    second = ExecutorProvider("second", selected)
    host = WindowsHost(executor_providers=(first, second))
    result = host.run("echo ok")
    assert result.stdout == b"ok"
    assert calls == ["selected"]


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="WindowsHost renders Windows PowerShell",
)
def test_windows_system_host_runs_with_local_executor_and_path():
    host = WindowsHost(
        executor_providers=(ExecutorProvider("local", LocalExecutor()),),
        path_providers=(PathProvider("local", lambda *parts: HostPath(*parts)),),
    )
    result = host.run(("echo", "ok"))
    assert result.returncode == 0
    assert host.path("tmp").name == "tmp"


def test_direct_path_command_without_argv_capability_renders_one_quoted_command():
    calls = []

    def execute(command, *args, **options):
        calls.append((command, args))
        return subprocess.CompletedProcess((command, *args), 0, b"ok", b"")

    host = PosixHost(executor_providers=(ExecutorProvider("shell", execute),))
    host.run(Exec("printf", "a & b"), check=False)

    assert len(calls) == 1
    rendered, args = calls[0]
    assert args == ()
    assert "'a & b'" in rendered
    assert ";" not in rendered


def test_direct_command_without_arguments_reaches_a_capabilityless_provider():
    """A bare callable provider advertises nothing, so the no-args direct
    branch renders the program itself through `command_text`."""
    calls = []

    def execute(command, *args, **options):
        calls.append((command, args))
        return subprocess.CompletedProcess((command, *args), 0, b"ok", b"")

    provider = ExecutorProvider("bare", execute, capabilities=())
    assert provider.capabilities == frozenset()

    host = PosixHost(executor_providers=(provider,))
    host.run(Exec("/bin/true"), check=False)
    assert calls[-1] == ("/bin/true", ())

    # A path program is asked for its filesystem representation, not str().
    host.run(Exec(PurePosixPath("/opt/tool")), check=False)
    assert calls[-1] == ("/opt/tool", ())

    # The sibling with-args branch still renders one quoted shell command.
    host.run(Exec("printf", "a & b"), check=False)
    rendered, args = calls[-1]
    assert args == ()
    assert "'a & b'" in rendered


def test_executor_fallback_replans_capabilities_before_retrying():
    calls = []

    def declined(command, *args, **options):
        calls.append(("first", command, args, options))
        raise OperationNotStartedError("offline before dispatch")

    def selected(command, *args, **options):
        calls.append(("second", command, args, options))
        return subprocess.CompletedProcess((command, *args), 0, b"ok", b"")

    first = ExecutorProvider("first", declined, capabilities=("args", "cwd"))
    second = ExecutorProvider("second", selected)
    host = PosixHost(executor_providers=(first, second))
    host.run(Exec("printf", "a & b"), cwd="/tmp", check=False)

    assert calls[0][0] == "first"
    assert calls[1][0] == "second"
    assert calls[1][2] == ()
    assert "'a & b'" in calls[1][1]


def test_ios_host_without_shell_requires_direct_provider():
    from hostctl.system._kinds import IOSHost

    host = IOSHost(executor_providers=(ExecutorProvider("raw", lambda *a, **k: None),))
    with pytest.raises(NotImplementedError):
        host.run("show version")


def test_provider_probe_is_cached_until_invalidation():
    calls = []
    provider = ExecutorProvider(
        "cached",
        lambda *a, **k: None,
        probe=lambda: calls.append(1) or ProviderProbe("available"),
    )
    selector = ProviderSelector((provider,))
    selector.select()
    selector.select()
    assert len(calls) == 1
    selector.invalidate()
    selector.select()
    assert len(calls) == 2


def test_path_provider_empty_capabilities_are_not_promoted():
    provider = PathProvider("none", lambda *parts: HostPath(*parts), capabilities=())
    assert provider.capabilities == frozenset()


def test_system_host_delegates_typed_runspace_to_capable_provider():
    marker = object()
    provider = ExecutorProvider(
        "psrp",
        lambda *args, **kwargs: None,
        capabilities=("runspace",),
    )
    provider.runspace = lambda: marker
    host = WindowsHost(executor_providers=(provider,))

    assert "runspace" in host.capabilities
    assert host.runspace() is marker


def test_system_host_resets_lazy_shell_and_closes_after_run_without_connect():
    shell_calls = []
    closed = []

    def resolve_shell():
        shell_calls.append(1)
        from hostctl.shell import POSIX_SHELL

        return POSIX_SHELL

    provider = ExecutorProvider(
        "fake",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, b"", b""),
    )
    provider.close = lambda: closed.append(1)
    host = PosixHost(executor_providers=(provider,), shell=resolve_shell)
    host.run("echo first", check=False)
    host.close()
    host.run("echo second", check=False)

    assert len(shell_calls) == 2
    assert closed == [1]


def test_system_host_connects_only_available_provider_and_closes_once():
    events = []
    first = ExecutorProvider(
        "offline",
        lambda *args, **kwargs: None,
        probe=lambda: ProviderProbe("unavailable", "offline"),
    )
    second = ExecutorProvider(
        "online",
        lambda *args, **kwargs: None,
    )
    second.connect = lambda: events.append("connect")
    second.close = lambda: events.append("close")
    host = PosixHost(executor_providers=(first, second))

    host.connect()
    host.close()
    host.close()
    assert events == ["connect", "close"]


def test_system_host_retries_pre_dispatch_connect_failure():
    events = []

    first = ExecutorProvider("first", lambda *args, **kwargs: None)
    first.connect = lambda: (_ for _ in ()).throw(
        OperationNotStartedError("connection unavailable")
    )
    second = ExecutorProvider("second", lambda *args, **kwargs: None)
    second.connect = lambda: events.append("second")
    host = PosixHost(executor_providers=(first, second))

    host.connect()
    assert events == ["second"]


def test_system_host_close_attempts_all_targets_and_reraises_first_error():
    events = []
    first = ExecutorProvider("first", lambda *args, **kwargs: None)
    second = ExecutorProvider("second", lambda *args, **kwargs: None)
    first.close = lambda: (_ for _ in ()).throw(RuntimeError("first close"))
    second.close = lambda: events.append("second")
    host = PosixHost(executor_providers=(first, second))
    host.connect()
    with pytest.raises(RuntimeError, match="first close"):
        host.close()
    assert events == ["second"]


def test_a_transport_config_opens_the_system_family_its_paths_select():
    from hostctl.transports.ssh._config import SSHConfig
    from hostctl.system._kinds import WindowsHost

    from pathapi import WindowsPathname

    assert isinstance(SSHConfig("host", pathname=WindowsPathname).open(), WindowsHost)
    assert isinstance(SSHConfig("host").open(), PosixHost)
    assert isinstance(
        WinRMConfig("host", username="user", password="x").open(), WindowsHost
    )


def test_transport_configs_compose_system_hosts_without_uri_changes():
    from hostctl.transports.ssh._config import SSHConfig
    from hostctl.transports.winrm._config import WinRMConfig

    ssh_config = SSHConfig("example", username="root")
    posix = ssh_config.open()
    assert posix.connection_uri == ssh_config.connection_uri
    assert posix.scheme == "ssh"
    winrm_config = WinRMConfig("example", username="admin", password="secret")
    windows = winrm_config.open()
    assert windows.connection_uri == winrm_config.connection_uri
    assert windows.scheme == "winrm"
    assert all(
        type(provider).__name__ != "_HostExecutorProvider"
        for provider in posix._executor_selector.providers
    )
    assert all(
        type(provider).__name__ != "_HostExecutorProvider"
        for provider in windows._executor_selector.providers
    )


def test_composite_path_accepts_path_protocol_and_retains_alternates():
    from hostctl.system._kinds import PosixHost

    first_backend = MemPathBackend()
    second_backend = MemPathBackend()
    first = PathProvider("first", lambda *parts: MemPath(*parts, backend=first_backend))
    second = PathProvider(
        "second", lambda *parts: MemPath(*parts, backend=second_backend)
    )
    host = PosixHost(path_providers=(first, second))

    child = host.path("root") / "child"
    alternate = child.via("second")

    assert str(alternate).replace("\\", "/").endswith("root/child")
    assert alternate.provider is second


def test_composite_path_uses_target_flavour_independent_of_client():
    from hostctl.system._kinds import PosixHost, WindowsHost

    posix_backend = MemPathBackend()
    posix_provider = PathProvider(
        "posix",
        lambda *parts: MemPath(*parts, backend=posix_backend),
    )
    posix = PosixHost(path_providers=(posix_provider,)).path(
        "/srv",
        r"name\with-backslash",
    )
    assert type(posix).__name__ == "CompositePosixPath"
    assert str(posix) == "/srv/name\\with-backslash"
    assert posix.parts[-1] == r"name\with-backslash"

    windows_backend = MemPathBackend()
    windows_provider = PathProvider(
        "windows",
        lambda *parts: MemPath(*parts, backend=windows_backend),
    )
    windows = WindowsHost(path_providers=(windows_provider,)).path(
        r"C:\\Users",
        "devuser",
    )
    assert type(windows).__name__ == "CompositeWindowsPath"
    assert str(windows) == r"C:\Users\devuser"
    assert windows.drive == "C:"
    assert windows.root == "\\"
    assert windows.parent.name == "Users"


def test_composite_path_keeps_logical_segments_when_backend_has_uri_identity():
    backend = MemPathBackend()

    def uri_backend(*parts):
        return MemPath("sftp:/example:22", *parts, backend=backend)

    provider = PathProvider("sftp", uri_backend)
    path = PosixHost(path_providers=(provider,)).path("/etc", "hosts")

    assert str(path) == "/etc/hosts"
    assert path.parts == ("/", "etc", "hosts")


def test_composite_path_routes_read_only_operations_and_pins_mutations():
    read_backend = MemPathBackend()
    write_backend = MemPathBackend()
    read_backend_path = MemPath("value", backend=read_backend)
    read_backend_path.write_bytes(b"read")
    read_caps = (
        "stat",
        "scandir",
        "open_read",
        "read",
        "exists",
        "is_file",
        "is_dir",
    )
    read = PathProvider(
        "read-only",
        lambda *parts: MemPath(*parts, backend=read_backend),
        capabilities=read_caps,
    )
    write = PathProvider(
        "write",
        lambda *parts: MemPath(*parts, backend=write_backend),
    )
    host = PosixHost(path_providers=(read, write))
    path = host.path("value")

    assert path.read_bytes() == b"read"
    path.write_bytes(b"written")
    assert path.provider is write
    assert path.read_bytes() == b"written"
    with path.open("rb") as stream:
        assert stream.read() == b"written"
    assert (path / "child").provider is write


def test_composite_path_rejects_cross_provider_rename():
    """Still rejected -- but as NotImplementedError, which `move()` acts on.

    `Path.move()` reads NotImplementedError as "rename cannot express this"
    and falls back to copy + remove; a ValueError aborted the move instead.
    """
    first = PathProvider(
        "first", lambda *parts: MemPath(*parts, backend=MemPathBackend())
    )
    second = PathProvider(
        "second", lambda *parts: MemPath(*parts, backend=MemPathBackend())
    )
    host = PosixHost(path_providers=(first, second))
    with pytest.raises(NotImplementedError, match="across path providers"):
        host.path("source").rename(host.path("target").via("second"))


def test_composite_iterdir_children_keep_the_provider_that_scanned_them():
    first = PathProvider(
        "first",
        lambda *parts: MemPath(*parts, backend=MemPathBackend()),
        capabilities=("stat",),
    )
    second_backend = MemPathBackend()
    MemPath("root", backend=second_backend).mkdir()
    MemPath("root/child", backend=second_backend).write_bytes(b"child")
    second = PathProvider(
        "second",
        lambda *parts: MemPath(*parts, backend=second_backend),
    )
    host = PosixHost(path_providers=(first, second))

    children = list(host.path("root").iterdir())
    assert [child.name for child in children] == ["child"]
    assert children[0].provider is second


def _argv_recording_provider(name="args", capabilities=("args", "cwd", "env")):
    """A provider that records the argv it is handed and reports success."""
    seen = []

    def execute(command, *args, **kwargs):
        seen.append((command, *args))
        return subprocess.CompletedProcess((command, *args), 0, "", "")

    return ExecutorProvider(name, execute, capabilities=capabilities), seen


def test_an_args_provider_is_given_one_shell_layer_not_two():
    """`flavour.command()` already invokes the shell; `invocation()` takes a script.

    Feeding the first to the second ran the target shell twice.
    """
    provider, seen = _argv_recording_provider()
    host = PosixHost(executor_providers=(provider,))

    host.run("echo hi", check=False)

    argv = seen[0]
    assert argv[:2] == ("/bin/sh", "-c")
    assert len(argv) == 3
    # The payload is the script itself, not another complete `sh -c ...` line.
    assert "-c" not in argv[2]


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None, reason="needs Windows PowerShell"
)
def test_windows_exit_status_survives_the_local_executor():
    """The double wrap lost every status, so check=True passed for a failure."""
    host = HostConfig("windows://node?executor=local")._create_host()

    assert host.run("cmd /c exit 3", check=False).returncode == 3
    with pytest.raises(subprocess.CalledProcessError):
        host.run("cmd /c exit 3", check=True)


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None, reason="needs Windows PowerShell"
)
def test_windows_quoting_and_variables_survive_the_local_executor():
    host = HostConfig("windows://node?executor=local")._create_host()

    assert host.run('Write-Output "a b"', check=False).stdout.strip() == b"a b"
    assert host.run("$v = 41 + 1; Write-Output $v", check=False).stdout.strip() == b"42"


def test_direct_exec_embeds_cwd_and_env_a_provider_cannot_carry():
    """The shipped SSH provider advertises nothing, so this was every SSH host.

    `cwd`/`env` were stripped from the options and only restored for a
    provider that carries them natively, and the direct branch never embedded
    what was left -- so the command ran in the wrong directory with the wrong
    environment and reported success.
    """
    calls = []

    def execute(command, *args, **options):
        calls.append((command, args, options))
        return subprocess.CompletedProcess((command, *args), 0, b"", b"")

    host = PosixHost(
        executor_providers=(ExecutorProvider("bare", execute, capabilities=()),)
    )

    host.run(Exec("/usr/bin/make"), cwd="/srv/build", env={"CC": "clang"}, check=False)

    command, args, options = calls[-1]
    script = shlex.split(command)[2]
    assert "cd -- /srv/build" in script
    assert "CC=clang" in script
    assert "/usr/bin/make" in script
    # Never smuggled back through the options a provider did not ask for.
    assert "cwd" not in options and "env" not in options


def test_direct_exec_quotes_a_program_the_remote_shell_would_split():
    """An SSH exec request is read by the remote login shell."""
    calls = []

    def execute(command, *args, **options):
        calls.append(command)
        return subprocess.CompletedProcess((command,), 0, b"", b"")

    host = PosixHost(
        executor_providers=(ExecutorProvider("bare", execute, capabilities=()),)
    )

    host.run(Exec("/opt/My Tools/run"), check=False)

    assert "'/opt/My Tools/run'" in calls[-1]


def test_an_args_provider_without_cwd_gets_the_context_in_the_script():
    """ARGS+ENV but no CWD silently ignored cwd=."""
    provider, seen = _argv_recording_provider(capabilities=("args", "env"))
    host = PosixHost(executor_providers=(provider,))

    host.run(Exec("/usr/bin/make"), cwd="/srv/build", check=False)

    argv = seen[-1]
    assert argv[:2] == ("/bin/sh", "-c")
    assert "/srv/build" in argv[2]
    assert "/usr/bin/make" in argv[2]


def test_a_shell_less_host_refuses_context_it_cannot_apply():
    """Better a clear refusal than a command that runs somewhere else.

    `IOSHost` configures no shell, so there is nowhere to embed cwd/env.
    """
    from hostctl.system._kinds import IOSHost

    provider = ExecutorProvider(
        "bare",
        lambda command, *args, **options: subprocess.CompletedProcess(
            (command,), 0, b"", b""
        ),
        capabilities=(),
    )
    host = IOSHost(executor_providers=(provider,))

    with pytest.raises(NotImplementedError, match="cannot apply cwd or env"):
        host.run(Exec("/bin/true"), cwd="/srv", check=False)


class _TransportProvider(PathProvider):
    """A path provider whose transport reconnects lazily, as SSH's does.

    The real transport does not reconnect when the host connects a provider;
    it reconnects behind whatever operation needs it, which is why a
    connection opened after `close()` has to be noticed some other way.
    """

    def __init__(self, backend, events):
        def factory(*parts):
            if self.closed:
                self.closed = False
                self.events.append("connect")
            return MemPath(*parts, backend=backend)

        super().__init__("mem", factory)
        self.events = events
        self.closed = False
        self.transport = self

    def connect(self):
        if self.closed:
            self.closed = False
        self.events.append("connect")

    def close(self):
        self.closed = True
        self.events.append("close")


def test_a_connection_reopened_through_path_is_closed_again():
    """`_closed_targets` was only cleared by connect()/run().

    `path()`, `spawn()` and `runspace()` dispatched straight to the provider,
    so a transport they reopened stayed marked closed and every later close()
    skipped it -- the connection lived until process exit, and repeated cycles
    piled up connections on the server.
    """
    events = []
    host = PosixHost(path_providers=(_TransportProvider(MemPathBackend(), events),))

    host.connect()
    host.close()
    host.path("x")  # the transport reconnects behind this
    host.close()

    assert events == ["connect", "close", "connect", "close"]


def test_the_initializer_runs_for_a_host_used_without_connect():
    """providers.md promises once per connection generation, not per connect().

    A host used lazily -- `run()`, `path()`, `spawn()` -- opened a generation
    with the documented bootstrap silently skipped.
    """
    ran = []
    host = PosixHost(
        executor_providers=(
            ExecutorProvider(
                "bare",
                lambda command, *args, **options: subprocess.CompletedProcess(
                    (command,), 0, b"", b""
                ),
                capabilities=(),
            ),
        ),
        initializer=SessionInitializer(lambda connected: ran.append(connected)),
    )

    host.run("systemctl restart app", check=False)

    assert ran == [host]

    host.run("systemctl status app", check=False)
    assert ran == [host], "the initializer must run once per generation"


def test_an_initializer_that_runs_a_command_does_not_recurse():
    """The initializer is handed the host and legitimately calls run()."""
    ran = []

    def bootstrap(connected):
        ran.append("start")
        connected.run("sudo -v", check=False)
        ran.append("done")

    host = PosixHost(
        executor_providers=(
            ExecutorProvider(
                "bare",
                lambda command, *args, **options: subprocess.CompletedProcess(
                    (command,), 0, b"", b""
                ),
                capabilities=(),
            ),
        ),
        initializer=bootstrap,
    )

    host.run("uptime", check=False)

    assert ran == ["start", "done"]


def test_two_hosts_from_one_config_do_not_share_a_transport():
    """The cache exists so ONE host's providers share a connection.

    Kept on the config it outlived the host, so a health check's `close()`
    tore down the SSH connection a worker was using mid-command. Uses the SSH
    descriptors because they are what the cache serves; `local` providers
    build no transport at all.
    """
    from hostctl.transports.ssh._config import SSHConfig

    config = PosixConfig(
        "node",
        executor=("ssh",),
        path=("sftp",),
        provider_options={"ssh": SSHConfig("node", username="root")},
    )

    first, second = config._create_host(), config._create_host()

    first_transport = first._executor_selector.providers[0].transport
    second_transport = second._executor_selector.providers[0].transport
    assert first_transport is not second_transport

    # The sharing the cache exists for still holds inside one host.
    assert first._path_selector.providers[0].transport is first_transport


def test_the_documented_initializer_example_does_not_raise():
    """`SessionInitializer(lambda h: ..., timeout=10)` is what providers.md shows.

    `timeout` was injected into the hook's keywords unconditionally, so that
    lambda raised TypeError at connect() and the connection was torn down.
    """
    ran = []
    host = PosixHost(
        executor_providers=(_InitProvider(),),
        initializer=SessionInitializer(
            lambda connected: ran.append(connected), timeout=10
        ),
    )

    host.connect()

    assert ran == [host]


def test_a_hook_that_declares_timeout_still_receives_it():
    seen = []
    host = PosixHost(
        executor_providers=(_InitProvider(),),
        initializer=SessionInitializer(
            lambda connected, timeout=None: seen.append(timeout), timeout=7
        ),
    )

    host.connect()

    assert seen == [7]


def test_a_system_uri_fails_closed_on_an_unknown_or_duplicated_parameter():
    """Every other config parses its query with `strict_uri_query`. This one
    used `parse_qsl` + `dict`, so `?exectuor=ssh` built a host with no
    executors that failed much later as "does not provide the 'run'
    capability", and a repeated `shell=` silently took the last value."""
    with pytest.raises(ValueError, match="unknown connection parameter: exectuor"):
        HostConfig("posix://node?exectuor=local")

    with pytest.raises(ValueError, match="duplicate connection parameter: shell"):
        HostConfig("posix://node?shell=bash&shell=zsh&executor=local")

    # The two that legitimately repeat still do.
    config = HostConfig("posix://node?executor=local&executor=ssh&path=local")
    assert config.executors == ("local", "ssh")


@pytest.mark.parametrize("authority", ("node", "node:22", "[::1]", "root@node:2222"))
def test_a_system_uri_round_trips_its_authority(authority):
    """`connection_uri` percent-encodes the authority while `_from_parsed_uri`
    stored the still-encoded netloc, so every `HostConfig(str(config))` added
    a layer: `node:22` -> `node%3A22` -> `node%253A22`."""
    config = PosixConfig(authority, executor=("local",))

    once = HostConfig(str(config))
    twice = HostConfig(str(once))

    assert once.authority == authority
    assert twice.authority == authority
    assert str(twice) == str(once)


def test_host_info_reports_a_hostname_not_a_whole_authority():
    """Userinfo in `HostInfo.hostname` put a username -- and a hint at the
    credential -- into every diagnostic that printed it."""
    host = HostConfig("posix://root@node:2222?executor=local")._create_host()

    assert host.info().hostname == "node"


def test_an_out_of_band_environment_is_carried_not_dropped():
    """`ShellCommand.environment` means "not embedded -- send it separately",
    which is what the SSH transport does. SystemHost passed only `.command`,
    so a registered flavour for a shell that cannot export variables lost
    every one of them in silence."""
    from hostctl.shell import POSIX_SHELL
    from hostctl.shell._vocabulary import ShellCommand

    class _OutOfBand(type(POSIX_SHELL)):
        name = "test-out-of-band"

        def command(self, cmds, *, executable=None, cwd=None, env=None):
            rendered = super().command(cmds, executable=executable, cwd=cwd, env=None)
            # A flavour for an appliance shell that cannot export variables:
            # it hands the environment back for the transport to carry.
            return ShellCommand(rendered.command, {"TZ": "UTC", **(env or {})})

    seen = []

    def execute(command, *args, **options):
        seen.append(options.get("env"))
        return subprocess.CompletedProcess((command,), 0, b"", b"")

    host = PosixHost(
        executor_providers=(
            ExecutorProvider("carries-env", execute, capabilities=("env",)),
        ),
        shell=_OutOfBand(),
    )

    host.run("printenv TZ", env={"TZ": "UTC"}, check=False)

    assert seen == [{"TZ": "UTC"}]


def test_a_provider_that_cannot_carry_it_refuses_rather_than_losing_it():
    from hostctl.shell import POSIX_SHELL
    from hostctl.shell._vocabulary import ShellCommand

    class _OutOfBand(type(POSIX_SHELL)):
        name = "test-out-of-band-2"

        def command(self, cmds, *, executable=None, cwd=None, env=None):
            rendered = super().command(cmds, executable=executable, cwd=cwd, env=None)
            return ShellCommand(rendered.command, dict(env or {}))

    host = PosixHost(
        executor_providers=(
            ExecutorProvider(
                "no-env",
                lambda command, *args, **options: subprocess.CompletedProcess(
                    (command,), 0, b"", b""
                ),
                capabilities=(),
            ),
        ),
        shell=_OutOfBand(),
    )

    with pytest.raises(NotImplementedError, match="out of band"):
        host.run("printenv TZ", env={"TZ": "UTC"}, check=False)


def test_a_family_with_no_path_grammar_says_so():
    """Everything non-Windows fell through to POSIX, so an `IOSHost` --
    documented as command-only until an IOS path grammar is designed --
    advertised `path` and applied POSIX rules to `flash:/config.text`."""
    from hostctl.system._kinds import IOSHost
    from pathapi import Path as LocalPath

    host = IOSHost(path_providers=(PathProvider("local", lambda *p: LocalPath(*p)),))

    with pytest.raises(NotImplementedError, match="command-only"):
        host.path("flash:/config.text")


def test_info_stops_dialling_once_it_knows_everything():
    """`info()` stops at the first provider that answers in full: dialling
    an ordered fallback may prompt for 2FA or wait out a TCP timeout merely
    to read `HostInfo` the primary already gave."""
    from hostctl.host._info import HostInfo

    connected = []

    class _Reporting(ExecutorProvider):
        def __init__(self, name, reply):
            super().__init__(
                name,
                lambda command, *args, **options: subprocess.CompletedProcess(
                    (command,), 0, b"", b""
                ),
            )
            self._reply = reply

        def connect(self):
            connected.append(self.name)

        def info(self):
            return self._reply

    full = HostInfo(
        hostname="node",
        os_family="posix",
        os_name="Linux",
        os_version="6.1",
        architecture="x86_64",
    )
    host = PosixHost(
        executor_providers=(_Reporting("primary", full), _Reporting("backup", full))
    )

    assert host.info().os_name == "Linux"
    assert connected == ["primary"]


def test_a_composed_hosts_executor_passes_argv_as_one_exec():
    """`executor("echo", "a")` is one program plus argv, not two commands."""
    calls = []

    def fake(command, *args, **options):
        calls.append((command, args))
        return subprocess.CompletedProcess(args, 0, b"", b"")

    host = PosixHost(
        executor_providers=(ExecutorProvider("fake", fake, capabilities=("args",)),)
    )
    executor = host.executor

    result = executor("echo", "a")

    assert result.returncode == 0
    assert calls == [("echo", ("a",))]
    assert executor.name == "fake"
    assert executor.executor_capabilities == frozenset({"args"})
    with pytest.raises(AttributeError):
        executor.no_such_attribute


def test_concurrent_hosts_from_one_config_do_not_share_a_transport(monkeypatch):
    """The transport cache lived on the shared config, so two threads opening
    hosts from it swapped each other's cache mid-build: one host's executor
    and path legs ended on different connections, or two hosts on one."""
    import threading
    import time

    from hostctl.transports.ssh._config import SSHConfig
    from hostctl.system import _resolvers as system_module

    real = system_module._ssh_pair

    def slow_ssh(host):
        time.sleep(0.02)  # widen the window between the two hosts' builds
        return real(host)

    # One resolver for both names, as the built-ins are: that is what makes
    # the executor and path legs of one host share a transport.
    monkeypatch.setitem(system_module._SYSTEM_PROVIDER_RESOLVERS, "ssh", slow_ssh)
    monkeypatch.setitem(system_module._SYSTEM_PROVIDER_RESOLVERS, "sftp", slow_ssh)
    config = PosixConfig(
        "node",
        executor=("ssh",),
        path=("sftp",),
        provider_options={"ssh": SSHConfig("example.invalid")},
    )
    hosts = [None, None]

    def build(index):
        hosts[index] = config._create_host()

    threads = [threading.Thread(target=build, args=(i,)) for i in range(2)]
    threads[0].start()
    time.sleep(0.005)
    threads[1].start()
    for thread in threads:
        thread.join()

    transports = []
    for host in hosts:
        executor = host._executor_selector.providers[0]
        path = host._path_selector.providers[0]
        assert executor.transport is path.transport
        transports.append(executor.transport)
    assert transports[0] is not transports[1]
    assert "_provider_transports" not in vars(config)


def test_a_system_host_takes_a_config_not_a_string_or_a_number():
    with pytest.raises(TypeError, match="Host\\(uri\\)"):
        PosixHost("ssh://root@nas")
    with pytest.raises(TypeError, match="int"):
        PosixHost(config=123)


def test_system_config_takes_a_sequence_of_descriptor_names():
    """`executor="local"` became `('l','o','c','a','l')`."""
    with pytest.raises(TypeError, match="sequence of descriptor names"):
        PosixConfig("n", executor="local")
    with pytest.raises(TypeError, match="sequence of descriptor names"):
        PosixConfig("n", path=b"local")


def test_system_config_refuses_an_unknown_keyword():
    with pytest.raises(TypeError, match="executr"):
        PosixConfig("n", executr=("local",))


def test_system_config_keeps_only_the_two_option_keys_it_reads():
    options = {"ssh": object()}
    config = PosixConfig("n", provider_options=options, initializer=len)

    assert config.options == {"provider_options": options, "initializer": len}
    assert PosixConfig("n").options == {}


def _probing(name, declared, probed):
    return ExecutorProvider(
        name,
        lambda command, *args, **options: subprocess.CompletedProcess(
            (command,), 0, b"", b""
        ),
        capabilities=declared,
        probe=lambda: ProviderProbe("available", capabilities=frozenset(probed)),
    )


def test_a_probe_narrows_the_declared_capabilities_everywhere():
    """A probe may narrow what a provider declared and must not invent any.
    `select`, `capabilities` and `provider_details` each read a different
    union of the two, so a narrowed `spawn` was still chosen and reported."""
    narrowed = _probing("narrowed", ("spawn", "tty"), ("args",))
    host = PosixHost(executor_providers=(narrowed,))

    with pytest.raises(OperationNotStartedError):
        host._executor_selector.select(capability="spawn")
    assert host.capabilities == frozenset({"run"})
    assert host.executor_capabilities == frozenset()
    (detail,) = host.provider_details
    assert detail["capabilities"] == ()
    with pytest.raises(NotImplementedError):
        host.spawn()


def test_a_probe_cannot_invent_a_capability():
    invented = _probing("invented", (), ("spawn",))
    host = PosixHost(executor_providers=(invented,))

    with pytest.raises(OperationNotStartedError):
        host._executor_selector.select(capability="spawn")
    assert "spawn" not in host.capabilities


def test_a_probe_that_states_nothing_leaves_the_declared_set():
    quiet = _probing("quiet", ("spawn",), ())
    selector = ProviderSelector((quiet,))

    assert selector.effective_capabilities(quiet) == frozenset({"spawn"})
    assert selector.select(capability="spawn").provider is quiet


def test_a_composite_path_honours_a_probe_that_narrows_away_write():
    backend = MemPathBackend()
    provider = PathProvider(
        "narrowing",
        lambda *parts: MemPath(*parts, backend=backend),
        probe=lambda: ProviderProbe(
            "available", capabilities=frozenset({"stat", "read", "open_read"})
        ),
    )
    host = PosixHost(path_providers=(provider,))
    target = host.path("/x")

    with pytest.raises(NotImplementedError):
        target.write_bytes(b"data")


def test_path_backend_probes_through_the_selector_once():
    calls = []

    def probe():
        calls.append(1)
        raise ConnectionError("probe blew up")

    provider = PathProvider(
        "a", lambda *parts: MemPath(*parts, backend=MemPathBackend()), probe=probe
    )
    host = PosixHost(path_providers=(provider,))

    for _ in range(2):
        with pytest.raises(OperationNotStartedError):
            host.path("/x", backend="a")

    assert len(calls) == 1


def test_path_backend_pins_every_operation_to_the_named_provider():
    """`backend=name` returns what `.via(name)` returns: a read, a write and a
    derived child all go to the named provider, never back to the first."""
    first_backend = MemPathBackend()
    named_backend = MemPathBackend()
    MemPath("/only-in-first", backend=first_backend).write_bytes(b"x")
    first = PathProvider("first", lambda *parts: MemPath(*parts, backend=first_backend))
    named = PathProvider("named", lambda *parts: MemPath(*parts, backend=named_backend))
    host = PosixHost(path_providers=(first, named))

    path = host.path("/only-in-first", backend="named")

    assert path.provider is named
    assert path.via("named") is path
    assert not path.exists()
    path.write_bytes(b"named")
    (path.parent / "child").write_bytes(b"child")
    assert MemPath("/only-in-first", backend=first_backend).read_bytes() == b"x"
    assert MemPath("/only-in-first", backend=named_backend).read_bytes() == b"named"
    assert MemPath("/child", backend=named_backend).read_bytes() == b"child"
    assert not MemPath("/child", backend=first_backend).exists()
    assert not host.path("/child").exists()


class _Transport:
    def __init__(self):
        self.closes = 0

    def close(self):
        self.closes += 1


def _executor(name, transport=None):
    provider = ExecutorProvider(
        name, lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b"")
    )
    provider.transport = transport if transport is not None else _Transport()
    return provider


def test_replace_providers_closes_the_old_transport_once_and_the_new_once():
    old, new = _executor("old"), _executor("new")
    host = PosixHost(executor_providers=(old,))
    host.run("true")
    assert [p.name for p in host.executor_providers] == ["old"]
    assert host.path_providers == ()

    host.replace_providers(executor=(new,))

    assert [p.name for p in host.executor_providers] == ["new"]
    assert old.transport.closes == 1
    host.run("true")
    host.close()
    assert (old.transport.closes, new.transport.closes) == (1, 1)


def test_replace_providers_keeps_a_transport_a_kept_provider_shares():
    shared = _Transport()
    old = _executor("old", shared)
    kept = PathProvider("kept", lambda *parts: MemPath(*parts))
    kept.transport = shared
    host = PosixHost(executor_providers=(old,), path_providers=(kept,))
    host.replace_providers(executor=(_executor("new"),))
    assert shared.closes == 0
    host.close()
    assert shared.closes == 1


def test_replace_providers_closes_a_shared_transport_once():
    shared = _Transport()
    executor = _executor("a", shared)
    path = PathProvider("a", lambda *parts: MemPath(*parts))
    path.transport = shared
    host = PosixHost(executor_providers=(executor,), path_providers=(path,))
    host.replace_providers(executor=(_executor("b"),), path=())
    assert shared.closes == 1


def test_replace_providers_with_duplicate_names_changes_nothing():
    old = _executor("old")
    host = PosixHost(executor_providers=(old,))
    with pytest.raises(ValueError):
        host.replace_providers(executor=(_executor("x"), _executor("x")))
    assert host.executor_providers == (old,)
    assert old.transport.closes == 0


def test_replace_providers_runs_the_initializer_again():
    ran = []
    kept = _executor("kept")
    host = PosixHost(
        executor_providers=(kept,), initializer=lambda connected: ran.append(1)
    )
    host.run("true")
    assert ran == [1]
    host.replace_providers(path=())
    host.run("true")
    assert ran == [1, 1]


def test_a_local_system_descriptor_uses_the_local_provider_classes():
    from hostctl.transports.local._providers import (
        LocalExecutorProvider,
        LocalPathProvider,
    )

    scheme = "windows" if os.name == "nt" else "posix"
    with HostConfig(f"{scheme}://node?executor=local&path=local").open() as host:
        assert isinstance(host.executor_providers[0], LocalExecutorProvider)
        assert isinstance(host.path_providers[0], LocalPathProvider)
