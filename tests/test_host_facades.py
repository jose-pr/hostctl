"""Compatibility transport hosts expose provider-based migration views."""

from hostctl import LocalConfig, LocalHost, SshConfig, SshHost, WinRMConfig, WinRMHost


def test_local_facade_builds_system_provider_view():
    host = LocalHost(LocalConfig())
    view = host.as_system_host()
    assert view.capabilities == frozenset(("path", "run"))
    assert str(view.path(".")) == "."


def test_remote_facades_build_views_without_connecting():
    ssh = SshHost(SshConfig("server"))
    winrm = WinRMHost(WinRMConfig("server", "user", password="secret"))
    assert ssh.as_system_host().config is ssh.config
    assert winrm.as_system_host().config is winrm.config
    assert "secret" not in winrm.as_system_host().connection_uri
