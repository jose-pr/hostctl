"""`ContainerConfig(verify=)` on an `https://` Engine, through the real docker-py.

The local HTTPS server answers docker-py's `/version` probe, which the client
sends while it is constructed: building the client is the handshake.
"""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")
docker = pytest.importorskip("docker")

from hostctl import ContainerConfig, HostConfig  # noqa: E402
from hostctl.transports.container import ContainerHost  # noqa: E402
from tls_server import (  # noqa: E402
    ThrowawayCA,
    TLSServer,
    certifi_holds,
    os_store_holds,
)

_ENV_BUNDLES = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")

_insecure = pytest.mark.filterwarnings(
    "ignore::urllib3.exceptions.InsecureRequestWarning"
)


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
    for name in _ENV_BUNDLES:
        monkeypatch.delenv(name, raising=False)


def _connects(server, **options) -> bool:
    host = ContainerHost(ContainerConfig("app", engine_url=server.url, **options))
    try:
        host.client.version()
        return True
    except docker.errors.DockerException as exc:
        assert "SSL" in str(exc), exc
        return False
    except Exception as exc:
        if "SSL" not in type(exc).__name__ and "SSL" not in str(exc):
            raise
        return False
    finally:
        host.close()


def test_the_default_verifies(tls):
    assert _connects(tls) is False


@pytest.mark.parametrize("which", ("bundle", "hashed"))
def test_a_bundle_is_trusted(tls, which):
    before = len(tls.requests)
    assert _connects(tls, verify=getattr(tls, which)) is True
    assert len(tls.requests) >= before + 2


def test_a_bundle_is_the_only_trust(tls):
    assert _connects(tls, verify=tls.other_bundle) is False


@_insecure
def test_false_checks_nothing(tls):
    assert _connects(tls, verify=False) is True


def test_an_environment_bundle_never_replaces_a_given_one(tls, monkeypatch):
    for name in _ENV_BUNDLES:
        monkeypatch.setenv(name, tls.bundle)
    assert _connects(tls, verify=tls.other_bundle) is False
    # docker-py's own default does honour it: that is `requests`' trust.
    assert _connects(tls) is True


@_insecure
def test_an_environment_bundle_does_not_switch_false_back_on(tls, monkeypatch):
    for name in _ENV_BUNDLES:
        monkeypatch.setenv(name, tls.other_bundle)
    assert _connects(tls, verify=False) is True


def test_a_missing_bundle_names_its_path_before_connecting(tls, tmp_path):
    before = len(tls.requests)
    with pytest.raises(FileNotFoundError, match="absent.pem"):
        _connects(tls, verify=str(tmp_path / "absent.pem"))
    assert len(tls.requests) == before


@pytest.mark.parametrize(
    "options",
    (
        {},
        {"engine_url": "tcp://127.0.0.1:2376"},
        {"engine_url": "unix:///var/run/docker.sock"},
        {"engine_url": "https://h:2376", "client_factory": lambda **kw: None},
    ),
)
@pytest.mark.parametrize("verify", (False, "ca.pem"))
def test_verify_needs_an_https_engine_the_config_builds(options, verify):
    with pytest.raises(ValueError, match="verify"):
        ContainerConfig("app", verify=verify, **options)
    assert ContainerConfig("app", **options).verify is True


@pytest.mark.parametrize("value", (None, 0, b"ca.pem"))
def test_verify_refuses_other_types(value):
    with pytest.raises(TypeError, match="verify"):
        ContainerConfig("app", engine_url="https://h:2376", verify=value)


@pytest.mark.parametrize("verify", (False, "/etc/docker/ca dir"))
def test_the_uri_key_round_trips(verify):
    config = ContainerConfig("app", engine_url="https://h:2376", verify=verify)
    uri = config.connection_uri
    assert "verify=" in uri
    again = HostConfig(uri)
    assert again.verify == verify
    assert again == config
    assert "verify" not in ContainerConfig("app").connection_uri


# `verify=True` on an `https://` engine: the OS store, simulated through the
# `_os_trust` seam (tls_server.os_store_holds), or a CA variable when set.


def test_true_trusts_the_os_store(tls, monkeypatch):
    os_store_holds(monkeypatch, tls.bundle)
    before = len(tls.requests)
    assert _connects(tls) is True
    assert len(tls.requests) >= before + 2


def test_true_does_not_consult_certifi(tls, monkeypatch):
    certifi_holds(monkeypatch, tls.bundle)
    os_store_holds(monkeypatch, tls.other_bundle)
    assert _connects(tls) is False


@pytest.mark.parametrize("name", _ENV_BUNDLES)
def test_a_ca_variable_replaces_the_os_store(tls, monkeypatch, name):
    os_store_holds(monkeypatch, tls.bundle)
    monkeypatch.setenv(name, tls.other_bundle)
    assert _connects(tls) is False
    os_store_holds(monkeypatch, tls.other_bundle)
    monkeypatch.setenv(name, tls.bundle)
    assert _connects(tls) is True


def test_a_bundle_ignores_the_os_store(tls, monkeypatch):
    os_store_holds(monkeypatch, tls.bundle)
    assert _connects(tls, verify=tls.other_bundle) is False


@_insecure
def test_false_ignores_the_os_store(tls, monkeypatch):
    os_store_holds(monkeypatch, tls.other_bundle)
    assert _connects(tls, verify=False) is True


def test_a_client_factory_keeps_its_own_trust(tls, monkeypatch):
    seen = []

    def factory(**options):
        seen.append(options)
        return docker.DockerClient(**options)

    os_store_holds(monkeypatch, tls.bundle)
    host = ContainerHost(
        ContainerConfig("app", engine_url=tls.url, client_factory=factory)
    )
    try:
        with pytest.raises(docker.errors.DockerException, match="SSL"):
            host.client
    finally:
        host.close()
    assert seen and seen[0]["base_url"] == tls.url
