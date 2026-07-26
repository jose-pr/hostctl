import subprocess

import pytest

from hostctl import (
    HostConfig,
    OperationNotStarted,
    PathProvider,
    ProviderProbe,
    ExecutorProvider,
    WindowsHost,
)
from hostctl.executor import LocalExecutor
from hostctl.host import HostPath


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


def test_ios_host_without_shell_requires_direct_provider():
    from hostctl import IosHost

    host = IosHost(executor_providers=(ExecutorProvider("raw", lambda *a, **k: None),))
    with pytest.raises(NotImplementedError):
        host.run("show version")
