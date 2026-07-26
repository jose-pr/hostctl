"""Shared Host identity, URI, capability, and information contracts."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from pathlib_next import WindowsPathname

from hostctl import (
    POWERSHELL,
    Host,
    HostConfig,
    HostInfo,
    LocalConfig,
    LocalHost,
    SshConfig,
    SshHost,
    WinRMConfig,
    WinRMHost,
)


class _UnsupportedHost(Host):
    config = LocalConfig()
    capabilities = frozenset()

    def info(self):
        return HostInfo()


class _ExternalHost(Host):
    capabilities = frozenset()

    def __init__(self, config):
        self.config = config

    def info(self):
        return HostInfo()


class _ExternalConfig(HostConfig, schemes=("external",)):
    def __init__(self, uri):
        self._connection_uri = uri

    @property
    def connection_uri(self):
        return self._connection_uri

    @classmethod
    def _matches_uri(cls, parsed):
        return parsed.scheme.casefold() in ("external", "external+special")

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials):
        if credentials:
            raise ValueError("external host does not accept credentials")
        return cls(parsed.geturl())

    def _create_host(self):
        return _ExternalHost(self)


def test_local_uri_round_trip():
    config = HostConfig("local:")
    assert isinstance(config, LocalConfig)
    assert str(config) == "local:"
    assert isinstance(HostConfig(str(config)), LocalConfig)
    assert str(HostConfig(str(config))) == str(config)
    assert isinstance(Host(str(config)).config, LocalConfig)
    with config as host:
        assert isinstance(host, LocalHost)
        assert host.connection_uri == "local:"
    with config.open() as host:
        assert isinstance(host, LocalHost)


def test_config_reentry_is_guarded_and_connect_failure_cleans_up():
    config = LocalConfig()
    with config:
        with pytest.raises(RuntimeError, match="already open"):
            config.__enter__()

    class _FailingHost(_UnsupportedHost):
        closed = False

        def connect(self):
            raise RuntimeError("connect failed")

        def close(self):
            self.closed = True

    class _FailingConfig(LocalConfig):
        def _create_host(self):
            self.host = _FailingHost()
            return self.host

    failing = _FailingConfig()
    with pytest.raises(RuntimeError, match="connect failed"):
        with failing:
            pass
    assert failing.host.closed
    assert failing._opened_host is None


def test_ssh_uri_round_trip_keeps_secrets_separate():
    source = SshHost(
        SshConfig(
            "windows.example.com",
            port=2222,
            username="admin",
            password="top-secret",
            executable="C:/Program Files/PowerShell/7/pwsh.exe",
            dialect="powershell",
            path_flavor=WindowsPathname,
        )
    )
    uri = source.connection_uri
    assert "top-secret" not in uri
    restored = Host(uri, password="top-secret", known_hosts=None)
    assert isinstance(restored, SshHost)
    assert restored.config.password == "top-secret"
    assert restored.config.dialect is POWERSHELL
    assert restored.config.path_flavor is WindowsPathname
    assert restored.config.executable == "C:/Program Files/PowerShell/7/pwsh.exe"
    assert restored.connection_uri == uri
    restored_config = HostConfig(uri, password="top-secret", known_hosts=None)
    assert isinstance(restored_config, SshConfig)
    assert str(restored_config) == uri


def test_winrms_uri_round_trip_keeps_secrets_separate():
    source = WinRMHost(
        WinRMConfig(
            "windows.example.com",
            "domain user",
            "top-secret",
            ssl=True,
            transport="kerberos",
            message_encryption="always",
            operation_timeout_sec=40,
            read_timeout_sec=50,
        )
    )
    uri = source.connection_uri
    assert "top-secret" not in uri
    restored = Host(uri, password="top-secret")
    assert isinstance(restored, WinRMHost)
    assert restored.scheme == "winrms"
    assert restored.config.username == "domain user"
    assert restored.config.password == "top-secret"
    assert restored.connection_uri == uri
    restored_config = HostConfig(uri, password="top-secret")
    assert isinstance(restored_config, WinRMConfig)
    assert str(restored_config) == uri


@pytest.mark.parametrize(
    "host",
    [
        LocalHost(),
        SshHost(SshConfig("host")),
        WinRMHost(WinRMConfig("host", "user")),
        WinRMHost(WinRMConfig("host", "user", ssl=True)),
    ],
)
def test_scheme_matches_connection_uri_and_registered_scheme(host):
    parsed = urlsplit(host.connection_uri)
    assert host.scheme == parsed.scheme.casefold()
    assert host.scheme in type(host.config)._uri_schemes
    assert type(host.config)._matches_uri(parsed)


@pytest.mark.parametrize(
    "uri, message",
    [
        ("ftp://host", "unsupported host scheme"),
        ("ssh://user:secret@host", "passwords must not appear"),
        ("ssh://host?dialect=posix&dialect=powershell", "duplicate"),
        ("ssh://host?guess_os=true", "unknown"),
        ("ssh:///tmp/socket", "requires a host"),
        ("winrm://host", "requires a username"),
        ("local:?dialect=posix", "exactly"),
    ],
)
def test_connection_string_rejects_invalid_or_ambiguous_input(uri, message):
    with pytest.raises(ValueError, match=message):
        Host(uri)


def test_external_subclass_scheme_and_custom_matcher_dispatch():
    assert isinstance(Host("external://target"), _ExternalHost)
    assert isinstance(Host("external+special://target"), _ExternalHost)


def test_entry_point_loading_is_lazy_and_cache_can_be_refreshed(monkeypatch):
    import hostctl.host._common as host_module

    class _EntryPoints:
        def select(self, **kwargs):
            assert kwargs == {"group": "hostctl.configs"}
            return (self,)

        @property
        def name(self):
            return "plugin"

        def load(self):
            class _PluginConfig(HostConfig, schemes=("plugin",)):
                def __init__(self, uri):
                    self._connection_uri = uri

                @property
                def connection_uri(self):
                    return self._connection_uri

                @classmethod
                def _from_parsed_uri(cls, parsed, **credentials):
                    return cls(parsed.geturl())

                def _create_host(self):
                    return _ExternalHost(self)

            return _PluginConfig

    HostConfig._refresh_uri_registry()
    monkeypatch.setattr(host_module._metadata, "entry_points", lambda: ())
    Host("local:")
    monkeypatch.setattr(host_module._metadata, "entry_points", lambda: _EntryPoints())
    with pytest.raises(ValueError, match="unsupported host scheme"):
        Host("plugin://target")
    HostConfig._refresh_uri_registry()
    try:
        assert Host("plugin://target").connection_uri == "plugin://target"
    finally:
        HostConfig._refresh_uri_registry()


def test_repr_redacts_credentials():
    assert "top-secret" not in repr(SshConfig("host", password="top-secret"))
    assert "top-secret" not in repr(WinRMConfig("host", "user", "top-secret"))


def test_capabilities_are_explicit():
    assert SshHost(SshConfig("host")).capabilities == frozenset(
        ("run", "path", "spawn", "tty")
    )
    assert WinRMHost(WinRMConfig("host", "user", "secret")).capabilities == frozenset(
        ("run", "path")
    )


def test_base_unsupported_operations_fail_immediately():
    host = _UnsupportedHost()
    with pytest.raises(NotImplementedError, match="'run' capability"):
        host.run("hostname")
    with pytest.raises(NotImplementedError, match="'path' capability"):
        host.path("file")
    with pytest.raises(NotImplementedError, match="'spawn' capability"):
        host.spawn("shell")


def test_local_info_uses_reported_values_without_guessing(monkeypatch):
    import hostctl.host._local as local_module

    monkeypatch.setattr(local_module._platform, "node", lambda: "")
    monkeypatch.setattr(local_module._platform, "system", lambda: "")
    monkeypatch.setattr(local_module._platform, "version", lambda: "")
    monkeypatch.setattr(local_module._platform, "machine", lambda: "")
    monkeypatch.setattr(local_module._os, "name", "")
    assert LocalHost().info() == HostInfo()


def test_ssh_info_parses_only_reported_fields():
    from test_host_remote import _Result, _host

    host, _ = _host(
        result=_Result(
            stdout=(
                b"hostname=server\nos_family=Linux\nos_name=debian\n"
                b"os_version=12\narchitecture=x86_64\n"
            )
        )
    )
    assert host.info() == HostInfo(
        hostname="server",
        os_family="linux",
        os_name="debian",
        os_version="12",
        architecture="x86_64",
    )


def test_winrm_info_parses_partial_response_without_guessing():
    from test_host_winrm import _Response, _host

    host, _ = _host(_Response(out=b"hostname=server\nos_family=Win32NT\n"))
    assert host.info() == HostInfo(hostname="server", os_family="windows")
