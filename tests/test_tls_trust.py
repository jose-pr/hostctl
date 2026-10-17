"""`host._tls`: what `verify=True` trusts on the `requests`-based legs.

"The OS store" is simulated by pointing the `_os_trust` seam at a throwaway
CA; the real machine's store is never touched. certifi is proven unused by
pointing `requests`' own default bundle at the server's CA: a session that
read it would accept the server.
"""

from __future__ import annotations

import ssl

import pytest

pytest.importorskip("cryptography")
requests = pytest.importorskip("requests")

from hostctl.host import _tls  # noqa: E402
from tls_server import ThrowawayCA, TLSServer, os_store_holds  # noqa: E402


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
    monkeypatch.setattr(requests.adapters, "DEFAULT_CA_BUNDLE_PATH", tls.bundle)
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
