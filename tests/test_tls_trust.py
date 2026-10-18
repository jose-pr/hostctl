"""`host._tls`: what `verify=True` trusts on the `requests`-based legs.

"The OS store" is simulated by adding a throwaway CA to the context the
`_os_trust` seam builds; the real machine's store is never touched. certifi is
proven unused by pointing `requests`' own default bundle at the server's CA: a
session that read it would accept the server.
"""

from __future__ import annotations

import os
import socket
import ssl
import sys
import threading

import pytest

pytest.importorskip("cryptography")
requests = pytest.importorskip("requests")

from hostctl.host import _tls  # noqa: E402
from tls_server import (  # noqa: E402
    ThrowawayCA,
    TLSServer,
    certifi_holds,
    os_store_holds,
    os_trust_built,
)

#: `1` reaches these; a comma-separated list names others.
_PUBLIC_HOSTS = ("pypi.org", "github.com", "www.python.org")
_PUBLIC = os.environ.get("HOSTCTL_TEST_PUBLIC_TLS", "")


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    directory = tmp_path_factory.mktemp("tls")
    ca = ThrowawayCA()
    other = ThrowawayCA("unrelated CA")
    with TLSServer(ca, directory) as server:
        server.bundle = ca.write_bundle(directory / "ca.pem")
        server.hashed = ca.write_hashed_dir(directory / "hashed")
        server.other_bundle = other.write_bundle(directory / "other.pem")
        yield server


@pytest.fixture(autouse=True)
def _no_env_bundle(monkeypatch):
    for name in _tls._ENV_BUNDLES:
        monkeypatch.delenv(name, raising=False)


def _get(server, session) -> bool:
    """True when `session` accepted the server's certificate."""
    try:
        session.get(server.url + "/version", timeout=10).raise_for_status()
        return True
    except requests.exceptions.SSLError:
        return False
    finally:
        session.close()


def _trusted() -> "requests.Session":
    session = requests.Session()
    _tls._trust_session(session)
    return session


def test_without_a_variable_the_os_store_is_the_trust(monkeypatch):
    marker = ssl.create_default_context()
    monkeypatch.setattr(_tls, "_os_trust", lambda: marker)
    assert _tls._default_context() is marker


@pytest.mark.parametrize("name", _tls._ENV_BUNDLES)
def test_a_variable_replaces_the_os_store(tls, monkeypatch, name):
    os_store_holds(monkeypatch, tls.bundle)
    monkeypatch.setenv(name, tls.other_bundle)
    assert _get(tls, _trusted()) is False
    monkeypatch.setenv(name, tls.bundle)
    assert _get(tls, _trusted()) is True


def test_requests_order_between_the_variables(tls, monkeypatch):
    os_store_holds(monkeypatch, tls.other_bundle)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", tls.bundle)
    monkeypatch.setenv("CURL_CA_BUNDLE", tls.other_bundle)
    assert _get(tls, _trusted()) is True
    # An empty variable is unset, as in `requests`.
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "")
    assert _get(tls, _trusted()) is False


def test_a_variable_may_name_a_hashed_directory(tls, monkeypatch):
    os_store_holds(monkeypatch, tls.other_bundle)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", tls.hashed)
    assert _get(tls, _trusted()) is True


def test_a_missing_variable_bundle_names_path_and_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("CURL_CA_BUNDLE", str(tmp_path / "absent.pem"))
    with pytest.raises(FileNotFoundError, match=r"CURL_CA_BUNDLE.*absent\.pem"):
        _tls._default_context()


def test_the_os_store_is_trusted(tls, monkeypatch):
    os_store_holds(monkeypatch, tls.bundle)
    assert _get(tls, _trusted()) is True


def test_certifi_is_not_consulted(tls, monkeypatch):
    certifi_holds(monkeypatch, tls.bundle)
    # The patch is effective: stock `requests` now trusts the server.
    assert _get(tls, requests.Session()) is True
    os_store_holds(monkeypatch, tls.other_bundle)
    assert _get(tls, _trusted()) is False


def test_nothing_is_loaded_into_the_context(tls, monkeypatch):
    context = ssl.create_default_context(cafile=tls.bundle)
    monkeypatch.setattr(_tls, "_os_trust", lambda: context)
    before = context.cert_store_stats()
    session = requests.Session()
    _tls._trust_session(session)
    assert session.get_adapter("https://")._hostctl_trust is context
    assert _get(tls, session) is True
    assert context.cert_store_stats() == before


def test_the_real_os_store_does_not_hold_the_throwaway_ca(tls):
    assert _get(tls, _trusted()) is False


def test_trusting_twice_keeps_the_first_context():
    adapter = requests.adapters.HTTPAdapter()
    first, second = ssl.create_default_context(), ssl.create_default_context()
    _tls._trust_adapter(adapter, first)
    _tls._trust_adapter(adapter, second)
    assert adapter.poolmanager.connection_pool_kw["ssl_context"] is first


def test_cert_verify_never_sets_a_ca_file():
    adapter = requests.adapters.HTTPAdapter()
    _tls._trust_adapter(adapter, ssl.create_default_context())

    class Pool:
        ca_certs = "preset.pem"
        ca_cert_dir = "preset"

    for verify, expected in ((True, "CERT_REQUIRED"), ("/x/ca.pem", "CERT_REQUIRED")):
        pool = Pool()
        adapter.cert_verify(pool, "https://h/", verify, None)
        assert (pool.cert_reqs, pool.ca_certs, pool.ca_cert_dir) == (
            expected,
            None,
            None,
        )
    pool = Pool()
    adapter.cert_verify(pool, "https://h/", False, None)
    assert pool.cert_reqs == "CERT_NONE"


def test_a_proxy_manager_gets_the_context():
    adapter = requests.adapters.HTTPAdapter()
    context = ssl.create_default_context()
    _tls._trust_adapter(adapter, context)
    manager = adapter.proxy_manager_for("http://proxy.invalid:3128")
    assert manager.connection_pool_kw["ssl_context"] is context
    adapter.close()


# Which context `verify=True` gets: the platform verifier where truststore is
# importable, OpenSSL's store otherwise, and a CA variable's bundle alone.


def test_the_os_trust_is_the_platform_verifier():
    truststore = pytest.importorskip("truststore")
    context = _tls._default_context()
    assert isinstance(context, truststore.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_without_truststore_openssl_loads_the_store(monkeypatch):
    monkeypatch.setitem(sys.modules, "truststore", None)
    context = _tls._default_context()
    assert type(context) is ssl.SSLContext
    assert context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.skipif(sys.version_info >= (3, 10), reason="truststore needs 3.10+")
def test_below_3_10_openssl_loads_the_store():
    assert type(_tls._default_context()) is ssl.SSLContext


@pytest.mark.parametrize("name", _tls._ENV_BUNDLES)
def test_a_variable_builds_a_plain_context_of_its_bundle(tls, monkeypatch, name):
    monkeypatch.setenv(name, tls.bundle)
    context = _tls._default_context()
    assert type(context) is ssl.SSLContext
    assert context.cert_store_stats()["x509_ca"] == 1


def test_the_platform_verifier_refuses_a_ca_the_os_does_not_trust(tls, monkeypatch):
    truststore = pytest.importorskip("truststore")
    built = os_trust_built(monkeypatch)
    assert _get(tls, _trusted()) is False
    assert len(built) == 1 and isinstance(built[0], truststore.SSLContext)


def test_the_platform_verifier_accepts_a_ca_the_os_trusts(tls, monkeypatch):
    truststore = pytest.importorskip("truststore")
    os_store_holds(monkeypatch, tls.bundle)
    session = _trusted()
    assert isinstance(
        session.get_adapter("https://")._hostctl_trust, truststore.SSLContext
    )
    assert _get(tls, session) is True


def test_nothing_is_loaded_into_the_platform_verifier(tls, monkeypatch):
    # truststore treats CAs loaded into its context as extra trust; nothing
    # on the way (requests, urllib3) may add any.
    truststore = pytest.importorskip("truststore")
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=tls.bundle)
    before = context._ctx.cert_store_stats()

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("CA certificates loaded into hostctl's context")

    for name in (
        "load_verify_locations",
        "load_default_certs",
        "set_default_verify_paths",
    ):
        monkeypatch.setattr(context, name, refuse)
    monkeypatch.setattr(_tls, "_os_trust", lambda: context)
    assert _get(tls, _trusted()) is True
    assert context._ctx.cert_store_stats() == before


def test_concurrent_handshakes_through_one_context_all_verify(tls):
    # truststore before 0.10.5 could leave a shared context unverifying when
    # handshakes overlapped; one session's context serves every thread.
    context = _tls._default_context()
    outcomes = []

    def connect() -> None:
        for _ in range(10):
            try:
                with socket.create_connection(("127.0.0.1", tls.port), 10) as raw:
                    with context.wrap_socket(raw, server_hostname="127.0.0.1"):
                        outcomes.append("accepted")
            except ssl.SSLCertVerificationError:
                outcomes.append("refused")

    threads = [threading.Thread(target=connect) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert outcomes == ["refused"] * 80
    assert context.verify_mode == ssl.CERT_REQUIRED


# Opt-in: a public host, verified with no CA variable. On Windows this is the
# case `ssl`'s own store can miss: a root Windows has not fetched yet.


@pytest.mark.skipif(
    not _PUBLIC, reason="HOSTCTL_TEST_PUBLIC_TLS unset (reaches the internet)"
)
@pytest.mark.parametrize(
    "host",
    [h.strip() for h in _PUBLIC.split(",") if h.strip() not in ("", "1")]
    or _PUBLIC_HOSTS,
)
def test_a_public_host_verifies_through_the_platform(host):
    truststore = pytest.importorskip("truststore")
    session = _trusted()
    assert isinstance(
        session.get_adapter("https://")._hostctl_trust, truststore.SSLContext
    )
    try:
        session.head(f"https://{host}/", timeout=30)
    finally:
        session.close()
