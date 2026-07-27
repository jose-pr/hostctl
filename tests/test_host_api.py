"""Shared Host identity, URI, capability, and information contracts."""

from __future__ import annotations

from urllib.parse import quote, urlsplit

import pytest
from pathlib_next import WindowsPathname

from hostctl import (
    POWERSHELL,
    Host,
    HostConfig,
    HostInfo,
    LocalConfig,
    LocalHost,
    PosixHost,
    WindowsHost,
    SshConfig,
    WinRMConfig,
    parse_credentials,
    redact_uri,
)
from hostctl.host._common import _encode_stripped_characters


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
    source = SshConfig(
        "windows.example.com",
        port=2222,
        username="admin",
        password="top-secret",
        executable="C:/Program Files/PowerShell/7/pwsh.exe",
        dialect="powershell",
        path_flavor=WindowsPathname,
    )
    source = source.open()
    assert isinstance(source, WindowsHost)
    uri = source.connection_uri
    assert "top-secret" not in uri
    restored = Host(uri, password="top-secret", known_hosts=None)
    assert isinstance(restored, WindowsHost)
    assert restored.config.password == "top-secret"
    assert restored.config.dialect is POWERSHELL
    assert restored.config.path_flavor is WindowsPathname
    assert restored.config.executable == "C:/Program Files/PowerShell/7/pwsh.exe"
    assert restored.connection_uri == uri
    restored_config = HostConfig(uri, password="top-secret", known_hosts=None)
    assert isinstance(restored_config, SshConfig)
    assert str(restored_config) == uri


def test_winrms_uri_round_trip_keeps_secrets_separate():
    source = WindowsHost.from_winrm(
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
    assert isinstance(restored, WindowsHost)
    assert restored.scheme == "winrms"
    assert restored.config.username == "domain user"
    assert restored.config.password == "top-secret"
    assert restored.connection_uri == uri
    restored_config = HostConfig(uri, password="top-secret")
    assert isinstance(restored_config, WinRMConfig)
    assert str(restored_config) == uri


def test_ssh_path_flavor_selects_system_semantics_without_changing_dialect():
    windows = Host(
        str(
            SshConfig(
                "windows.example", path_flavor=WindowsPathname, dialect="powershell"
            )
        )
    )
    assert isinstance(windows, WindowsHost)
    assert windows.shell_flavour is POWERSHELL
    posix_pwsh = Host(str(SshConfig("posix.example", dialect="powershell")))
    assert isinstance(posix_pwsh, PosixHost)
    assert posix_pwsh.shell_flavour is POWERSHELL


def test_transport_system_hosts_have_safe_info_fallback_without_connection():
    ssh = Host("ssh://root@example.com")
    winrm = Host("winrm://user@example.com")
    assert ssh.info().hostname == "example.com"
    assert winrm.info().hostname == "example.com"


@pytest.mark.parametrize(
    "host",
    [
        LocalHost(),
        PosixHost.from_ssh(SshConfig("host")),
        WindowsHost.from_winrm(WinRMConfig("host", "user")),
        WindowsHost.from_winrm(WinRMConfig("host", "user", ssl=True)),
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


def test_uri_password_is_extracted_and_kept_out_of_the_canonical_form():
    config = HostConfig("ssh://admin:hunter2@nas.example.com:22")

    # The password is used...
    assert config.password == "hunter2"
    # ...but never rendered back out.
    assert "hunter2" not in config.connection_uri
    assert "hunter2" not in repr(config)
    assert "hunter2" not in str(config)
    assert config.connection_uri.startswith("ssh://admin@nas.example.com:22")


def test_uri_password_is_percent_decoded():
    config = HostConfig("ssh://admin:p%40ss%3Aword@host")

    assert config.password == "p@ss:word"


def test_uri_password_conflicting_with_an_argument_is_rejected():
    with pytest.raises(ValueError, match="both in the connection URI"):
        HostConfig("ssh://admin:fromuri@host", password="fromarg")


def test_redact_uri_strips_the_password_leaving_a_reusable_uri():
    redacted = redact_uri("ssh://admin:hunter2@nas.example.com:22?dialect=posix")

    assert "hunter2" not in redacted
    # The password is removed, not masked: a placeholder would round-trip into
    # a wrong credential if the rendered form were fed back in.
    assert "*" not in redacted
    assert redacted == "ssh://admin@nas.example.com:22?dialect=posix"
    # Still valid input, and it reconstructs the same configuration.
    assert urlsplit(redacted).hostname == "nas.example.com"
    assert HostConfig(redacted).username == "admin"


def test_redact_uri_output_carries_no_credential_back_in():
    config = HostConfig(redact_uri("ssh://admin:hunter2@nas.example.com"))

    assert config.password is None


@pytest.mark.parametrize(
    "value, expected",
    [
        ("hunter2", ("hunter2", {})),
        ("hunter2\notp:123456", ("hunter2", {"otp": "123456"})),
        (
            "hunter2\notp:123456\nrealm:CORP",
            ("hunter2", {"otp": "123456", "realm": "CORP"}),
        ),
        # A CRLF-terminated value must not leave "\r" on the password.
        ("hunter2\r\notp:1", ("hunter2", {"otp": "1"})),
        # Only the first ":" separates; a value may contain more.
        ("pw\nurl:https://host:8080/x", ("pw", {"url": "https://host:8080/x"})),
        # Names are casefolded and stripped; blank lines are ignored.
        ("pw\n\n  Otp  :123456", ("pw", {"otp": "123456"})),
        # Tabs and mixed whitespace around a name are stripped too.
        ("pw\n\tOtp \t:1", ("pw", {"otp": "1"})),
        ("pw\n   interactive   ", ("pw", {"interactive": ""})),
        # Values are NOT trimmed: a secret may legitimately begin or end with
        # whitespace, and silently stripping it would fail authentication with
        # no visible cause.
        ("pw\notp : 123456 ", ("pw", {"otp": " 123456 "})),
        # A bare name is a flag: it means the same as "name:".
        ("pw\ninteractive", ("pw", {"interactive": ""})),
        ("pw\nflag:", ("pw", {"flag": ""})),
        ("pw\ninteractive\notp:1", ("pw", {"interactive": "", "otp": "1"})),
        ("pw\r\nInteractive\r\nOtp:9", ("pw", {"interactive": "", "otp": "9"})),
    ],
)
def test_parse_credentials_splits_extras_after_the_password(value, expected):
    assert parse_credentials(value) == expected


def test_parse_credentials_rejects_an_empty_extra_name():
    with pytest.raises(ValueError, match="must not be empty"):
        parse_credentials("pw\n:novalue")


@pytest.mark.parametrize(
    "uri",
    [
        # A deleted character in the authority rewrites the target, so a URI
        # that reads as one host would connect to another. No encoding makes
        # that meaningful, so it is refused.
        "ssh://admin:pw@evil.example\n.trusted.example/",
        "ssh://admin:pw@evil.example\r.trusted.example/",
        "ssh://admin:pw@evil\t.example/",
    ],
)
def test_uri_rejects_control_characters_in_the_host(uri):
    with pytest.raises(ValueError, match="host contains a control character"):
        HostConfig(uri)


def test_uri_password_may_contain_a_raw_newline():
    # `urlsplit` would delete a raw newline, silently turning the credential
    # extras into part of the password ("pw\notp:1" -> "pwotp:1"). Encoding it
    # before parsing means a caller can write the separator naturally -- a
    # password read from a file or a prompt carries a real newline.
    raw = "ssh://admin:hunter2\notp:123456@nas.example.com"

    # The extra reaches the config by name rather than being swallowed.
    with pytest.raises(ValueError, match="otp"):
        HostConfig(raw)

    # The host is untouched by the encoding.
    assert urlsplit(_encode_stripped_characters(raw)).hostname == "nas.example.com"


def test_uri_password_raw_and_encoded_newlines_parse_identically():
    raw = "ssh://admin:hunter2\notp:1@host"
    encoded = "ssh://admin:hunter2%0Aotp:1@host"

    assert _encode_stripped_characters(raw) == encoded
    # Both spellings yield the same password field, so a caller may use either.
    assert (
        urlsplit(_encode_stripped_characters(raw)).password
        == urlsplit(encoded).password
    )


def test_redact_uri_handles_a_raw_newline_without_raising():
    # Redaction is used in log records and error messages, where refusing to
    # render a diagnostic would be worse than rendering an odd one.
    assert (
        redact_uri("ssh://admin:pw\notp:1@nas.example.com")
        == "ssh://admin@nas.example.com"
    )


def test_ssh_providers_share_one_transport_for_composition():
    from hostctl import ssh_providers
    from hostctl.provider import ExecutorProvider, PathProvider

    executor, path = ssh_providers(SshConfig("nas.example.com", username="root"))

    assert isinstance(executor, ExecutorProvider)
    assert isinstance(path, PathProvider)
    # Sharing the transport is the invariant this factory exists to enforce:
    # assembling the pair by hand can silently open two connections, only one
    # of which is ever closed.
    assert executor.transport is path.transport


def test_winrm_providers_share_one_transport_for_composition():
    from hostctl import winrm_providers
    from hostctl.provider import ExecutorProvider, PathProvider

    executor, path = winrm_providers(
        WinRMConfig("win.example.com", "admin", password="x")
    )

    assert isinstance(executor, ExecutorProvider)
    assert isinstance(path, PathProvider)
    assert executor.transport is path.transport


def test_uri_password_field_carries_credential_extras():
    quoted = quote("hunter2\notp:123456", safe="")

    with pytest.raises(ValueError, match="otp"):
        # SshConfig declares no `otp` credential, so the extra is rejected by
        # name rather than silently dropped.
        HostConfig(f"ssh://admin:{quoted}@nas.example.com")


@pytest.mark.parametrize(
    "uri",
    [
        "ssh://admin@host",
        "local:",
        "ssh://host?dialect=posix",
    ],
)
def test_redact_uri_leaves_a_password_free_uri_unchanged(uri):
    assert redact_uri(uri) == uri


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
    assert PosixHost.from_ssh(SshConfig("host")).capabilities == frozenset(
        ("run", "path")
    )
    windows_capabilities = WindowsHost.from_winrm(
        WinRMConfig("host", "user", "secret")
    ).capabilities
    assert {"run", "path"}.issubset(windows_capabilities)


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
