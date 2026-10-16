"""`WinRMConfig(verify=)`: True, False, or a CA bundle, honoured by every leg.

The pywinrm and PSRP legs are driven against a local HTTPS server whose
certificate comes from a throwaway CA, through the real libraries, so a
bundle that is not actually used -- or a check that is silently off -- fails
here. The server answers 401: getting it means the handshake was accepted.
"""

from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("cryptography")

from hostctl import HostConfig, WinRMConfig  # noqa: E402
from hostctl.transports.winrm._native import NativeWinRMSession  # noqa: E402
from tls_server import ThrowawayCA, TLSServer  # noqa: E402

_ENV_BUNDLES = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    directory = tmp_path_factory.mktemp("tls")
    ca = ThrowawayCA()
    other = ThrowawayCA("unrelated CA")
    with TLSServer(ca, directory) as server:
        server.bundle = ca.write_bundle(directory / "ca.pem")
        server.hashed = ca.write_hashed_dir(directory / "hashed")
        server.other_bundle = other.write_bundle(directory / "other.pem")
        server.other_hashed = other.write_hashed_dir(directory / "other-hashed")
        yield server


@pytest.fixture(autouse=True)
def _no_env_bundle(monkeypatch):
    for name in _ENV_BUNDLES:
        monkeypatch.delenv(name, raising=False)


def _config(server, **options) -> WinRMConfig:
    options.setdefault("password", "secret")
    options.setdefault("auth", "basic")
    return WinRMConfig(
        "127.0.0.1",
        username="user",
        tls=True,
        port=server.port,
        **options,
    )


def _pywinrm(config):
    """Send one WS-Man message through the pywinrm leg; True when TLS was accepted."""
    pytest.importorskip("winrm")
    import requests
    from winrm.exceptions import InvalidCredentialsError

    from hostctl.transports.winrm._transport import _PywinrmLeg

    protocol = _PywinrmLeg(config)._build()._protocol
    try:
        protocol.transport.send_message("<x/>")
    except requests.exceptions.SSLError:
        return False
    except InvalidCredentialsError:
        return True
    finally:
        protocol.transport.close_session()
    raise AssertionError("the 401 the server always answers did not raise")


def _psrp(config):
    """Send one WS-Man message through the PSRP leg; True when TLS was accepted."""
    pytest.importorskip("pypsrp")
    import requests
    from pypsrp.exceptions import AuthenticationError

    from hostctl.transports.winrm._psrp import RunspaceSession

    pool = RunspaceSession(config)._make_pool()
    transport = pool.connection.transport
    try:
        transport.send(b"<x/>")
    except requests.exceptions.SSLError:
        return False
    except AuthenticationError:
        return True
    finally:
        transport.close()
    raise AssertionError("the 401 the server always answers did not raise")


_LEGS = [
    pytest.param(_pywinrm, id="pywinrm"),
    pytest.param(
        _psrp,
        id="psrp",
        marks=pytest.mark.skipif(
            sys.version_info < (3, 10), reason="hostctl[psrp] needs Python 3.10+"
        ),
    ),
]


@pytest.mark.parametrize("send", _LEGS)
def test_the_default_verifies(tls, send):
    assert _config(tls).verify is True
    assert send(_config(tls)) is False


@pytest.mark.parametrize("send", _LEGS)
@pytest.mark.parametrize("which", ("bundle", "hashed"))
def test_a_bundle_is_trusted(tls, send, which):
    before = len(tls.requests)
    assert send(_config(tls, verify=getattr(tls, which))) is True
    assert len(tls.requests) > before


@pytest.mark.parametrize("send", _LEGS)
@pytest.mark.parametrize("which", ("other_bundle", "other_hashed"))
def test_a_bundle_is_the_only_trust(tls, send, which):
    # The server's CA is in neither, so accepting it would mean the bundle
    # was replaced by some other trust source.
    assert send(_config(tls, verify=getattr(tls, which))) is False


@pytest.mark.parametrize("send", _LEGS)
def test_false_is_the_one_value_that_checks_nothing(tls, send):
    assert send(_config(tls, verify=False)) is True
    for value in (True, tls.other_bundle, tls.other_hashed):
        assert send(_config(tls, verify=value)) is False, value


@pytest.mark.parametrize("send", _LEGS)
def test_an_environment_bundle_never_replaces_a_given_one(tls, send, monkeypatch):
    for name in _ENV_BUNDLES:
        monkeypatch.setenv(name, tls.bundle)
    assert send(_config(tls, verify=tls.other_bundle)) is False


@pytest.mark.parametrize("send", _LEGS)
def test_an_environment_bundle_does_not_switch_false_back_on(tls, send, monkeypatch):
    for name in _ENV_BUNDLES:
        monkeypatch.setenv(name, tls.other_bundle)
    assert send(_config(tls, verify=False)) is True


@pytest.mark.parametrize("send", _LEGS)
def test_a_missing_bundle_names_its_path_before_connecting(tls, send, tmp_path):
    missing = str(tmp_path / "absent.pem")
    before = len(tls.requests)
    with pytest.raises(FileNotFoundError, match="absent.pem"):
        send(_config(tls, verify=missing))
    assert len(tls.requests) == before


def test_native_refuses_a_bundle(tls):
    with pytest.raises(ValueError, match="Windows certificate store"):
        _config(tls, password=None, provider="native", verify=tls.bundle)
    with pytest.raises(ValueError, match="Windows certificate store"):
        NativeWinRMSession("h", ssl=True, verify=tls.bundle)


@pytest.mark.skipif(os.name != "nt", reason="the native leg is Windows-only")
def test_native_chosen_by_auto_refuses_a_bundle_before_running(tls, monkeypatch):
    from hostctl.transports.winrm._transport import _NativeLeg, _select_leg

    monkeypatch.setenv("USERNAME", "user")
    monkeypatch.delenv("USERDOMAIN", raising=False)
    config = _config(tls, password=None, auth="ntlm", verify=tls.bundle)
    assert _select_leg(config) == "native"
    with pytest.raises(ValueError, match="Windows certificate store"):
        _NativeLeg(config)._build()


def test_native_skips_the_checks_only_for_false():
    def options(verify):
        return NativeWinRMSession("h", ssl=True, verify=verify)._wrapper("x")

    assert "-SkipCACheck -SkipCNCheck -SkipRevocationCheck" in options(False)
    assert "SkipCACheck" not in options(True)


@pytest.mark.parametrize("value", (None, 0, 1, b"ca.pem", ["ca.pem"]))
def test_verify_refuses_other_types(value):
    with pytest.raises(TypeError, match="verify"):
        WinRMConfig("h", username="u", password="p", tls=True, verify=value)


@pytest.mark.parametrize("value", ("", "false", "False", "0", "no", "off", "true"))
def test_verify_refuses_a_string_that_reads_as_a_boolean(value):
    with pytest.raises(ValueError, match="verify"):
        WinRMConfig("h", username="u", password="p", tls=True, verify=value)


def test_a_bundle_needs_tls():
    with pytest.raises(ValueError, match="tls=True"):
        WinRMConfig("h", username="u", password="p", verify="ca.pem")
    # False over plain HTTP is harmless: there is no certificate either way.
    assert WinRMConfig("h", username="u", password="p", verify=False).verify is False


def test_a_path_like_is_stored_as_text_and_round_trips(tmp_path):
    bundle = tmp_path / "my ca&x=1.pem"
    config = WinRMConfig("h", username="u", password="p", tls=True, verify=bundle)
    assert config.verify == str(bundle)
    assert isinstance(config.verify, str)
    uri = config.connection_uri
    assert "verify=" in uri and "my ca" not in uri
    again = HostConfig(uri, password="p")
    assert again.verify == str(bundle)
    assert again == config


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ("", True),
        ("&verify=true", True),
        ("&verify=1", True),
        ("&verify=FALSE", False),
        ("&verify=no", False),
        ("&verify=C%3A%5Cca%20dir%5Cca.pem", "C:\\ca dir\\ca.pem"),
        ("&verify=%2Fetc%2Fssl%2Fcerts", "/etc/ssl/certs"),
    ),
)
def test_the_uri_key(query, expected):
    config = HostConfig(f"winrms://u@h?auth=ntlm{query}", password="p")
    assert config.verify == expected
    assert HostConfig(config.connection_uri, password="p").verify == expected


def test_an_empty_uri_value_is_refused():
    with pytest.raises(ValueError, match="verify"):
        HostConfig("winrms://u@h?verify=", password="p")


def test_a_uri_bundle_over_http_is_refused():
    with pytest.raises(ValueError, match="tls=True"):
        HostConfig("winrm://u@h?verify=%2Fca.pem", password="p")


def test_the_bundle_reaches_each_library_by_its_own_keyword(monkeypatch, tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_bytes(b"")
    winrm = pytest.importorskip("winrm")
    seen = {}

    class Session:
        def __init__(self, endpoint, **kwargs):
            seen.update(kwargs)
            self.protocol = type("P", (), {"transport": type("T", (), {})()})()

    monkeypatch.setattr(winrm, "Session", Session)
    from hostctl.transports.winrm._transport import _PywinrmLeg

    config = WinRMConfig("h", username="u", password="p", tls=True, verify=bundle)
    _PywinrmLeg(config)._build()
    assert seen["ca_trust_path"] == str(bundle)
    assert seen["server_cert_validation"] == "validate"

    seen.clear()
    _PywinrmLeg(WinRMConfig("h", username="u", password="p"))._build()
    assert "ca_trust_path" not in seen
    assert seen["server_cert_validation"] == "validate"


def test_pathlib_bundle_on_the_psrp_mapping(tmp_path):
    from hostctl.transports.winrm._psrp import _cert_validation

    bundle = tmp_path / "ca.pem"
    bundle.write_bytes(b"")
    assert _cert_validation(str(bundle)) == str(bundle)
    assert _cert_validation(True) is True
    assert _cert_validation(False) is False
    # A duck-typed config's falsy value is not a request to switch checks off.
    assert _cert_validation(None) is True
    assert _cert_validation(0) is True
