import subprocess

import pytest
from pathlib_next.mempath import MemPath, MemPathBackend

from hostctl import (
    HostConfig,
    OperationNotStarted,
    PathProvider,
    ProviderProbe,
    ExecutorProvider,
    PosixHost,
    PosixConfig,
    WindowsHost,
    WinRMConfig,
    ProviderSelector,
    SessionInitializer,
)
from hostctl.executor import LocalExecutor
from hostctl.host import HostPath


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
    assert host._executor_selector.last_selection is None


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
    from hostctl import SshConfig

    config = PosixConfig(
        "node",
        executor=("ssh",),
        path=("sftp",),
        provider_options={"ssh": SshConfig("node", username="root")},
    )
    host = config._create_host()
    assert host.capabilities == frozenset(("run", "path"))


def test_provider_selector_rejects_unavailable_without_replay():
    calls = []

    def declined(*args, **kwargs):
        calls.append("declined")
        raise OperationNotStarted("preflight")

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
    host.run(HostPath("printf"), "a & b", check=False)

    assert len(calls) == 1
    rendered, args = calls[0]
    assert args == ()
    assert "'a & b'" in rendered
    assert ";" not in rendered


def test_executor_fallback_replans_capabilities_before_retrying():
    calls = []

    def declined(command, *args, **options):
        calls.append(("first", command, args, options))
        raise OperationNotStarted("offline before dispatch")

    def selected(command, *args, **options):
        calls.append(("second", command, args, options))
        return subprocess.CompletedProcess((command, *args), 0, b"ok", b"")

    first = ExecutorProvider("first", declined, capabilities=("args", "cwd"))
    second = ExecutorProvider("second", selected)
    host = PosixHost(executor_providers=(first, second))
    host.run(HostPath("printf"), "a & b", cwd="/tmp", check=False)

    assert calls[0][0] == "first"
    assert calls[1][0] == "second"
    assert calls[1][2] == ()
    assert "'a & b'" in calls[1][1]


def test_ios_host_without_shell_requires_direct_provider():
    from hostctl import IosHost

    host = IosHost(executor_providers=(ExecutorProvider("raw", lambda *a, **k: None),))
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
        from hostctl import POSIX_SHELL

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
        OperationNotStarted("connection unavailable")
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


def test_transport_facade_helpers_validate_system_family():
    from hostctl import PosixHost, SshConfig, WindowsHost

    from pathlib_next import WindowsPathname

    with pytest.raises(ValueError, match="selects"):
        PosixHost.from_ssh(SshConfig("host", path_flavor=WindowsPathname))
    assert isinstance(WindowsHost.from_winrm(WinRMConfig("host", "user")), WindowsHost)


def test_transport_configs_compose_system_hosts_without_uri_changes():
    from hostctl import PosixHost, SshConfig, WindowsHost, WinRMConfig

    ssh_config = SshConfig("example", username="root")
    posix = PosixHost.from_ssh(ssh_config)
    assert posix.connection_uri == ssh_config.connection_uri
    assert posix.scheme == "ssh"
    winrm_config = WinRMConfig("example", "admin", password="secret")
    windows = WindowsHost.from_winrm(winrm_config)
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
    from hostctl import PosixHost

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
    from hostctl import PosixHost, WindowsHost

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
        "jose",
    )
    assert type(windows).__name__ == "CompositeWindowsPath"
    assert str(windows) == r"C:\Users\jose"
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
    first = PathProvider(
        "first", lambda *parts: MemPath(*parts, backend=MemPathBackend())
    )
    second = PathProvider(
        "second", lambda *parts: MemPath(*parts, backend=MemPathBackend())
    )
    host = PosixHost(path_providers=(first, second))
    with pytest.raises(ValueError, match="across path providers"):
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
