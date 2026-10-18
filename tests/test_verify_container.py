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
    os_trust_built,
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


def test_true_checks_with_the_platform_verifier(tls, monkeypatch):
    truststore = pytest.importorskip("truststore")
    built = os_trust_built(monkeypatch)
    assert _connects(tls) is False
    os_store_holds(monkeypatch, tls.bundle)
    assert _connects(tls) is True
    assert built
    assert all(isinstance(context, truststore.SSLContext) for context in built)


@_insecure
def test_a_bundle_or_false_never_builds_the_default_trust(tls, monkeypatch):
    from hostctl.host import _tls

    def refuse() -> None:
        raise AssertionError("verify=True's context built for another value")

    monkeypatch.setattr(_tls, "_default_context", refuse)
    assert _connects(tls, verify=tls.bundle) is True
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


# -- client_cert: a daemon run with --tlsverify --------------------------------


@pytest.fixture(scope="module")
def mtls(tmp_path_factory):
    """A server that requires a client certificate from `client_ca`."""
    directory = tmp_path_factory.mktemp("mtls")
    ca = ThrowawayCA()
    client_ca = ThrowawayCA("client CA")
    stranger = ThrowawayCA("unrelated client CA")
    with TLSServer(ca, directory, client_ca=client_ca) as server:
        server.bundle = ca.write_bundle(directory / "ca.pem")
        server.client_cert = client_ca.write_client_cert(directory)
        server.stranger_cert = stranger.write_client_cert(directory, "stranger")
        yield server


def _handshakes(server, **options) -> bool:
    """Whether a request got past the handshake: the server records only those.

    Under TLS 1.3 the server rejects a client certificate after the client's
    side of the handshake has finished, so the client sees a reset or an SSL
    alert on its first read; the server's record is what tells them apart.
    """
    before = len(server.requests)
    host = ContainerHost(ContainerConfig("app", engine_url=server.url, **options))
    try:
        host.client.version()
    except docker.errors.DockerException as exc:
        # docker-py wraps the reset or the SSL error; anything else is a bug.
        assert (
            "SSL" in str(exc)
            or "reset" in str(exc).casefold()
            or ("forcibly closed" in str(exc))
        ), exc
        assert len(server.requests) == before
        return False
    finally:
        host.close()
    assert len(server.requests) > before
    return True


def test_without_a_client_certificate_the_handshake_fails(mtls):
    assert _handshakes(mtls, verify=mtls.bundle) is False


def test_the_right_client_certificate_is_presented(mtls):
    assert _handshakes(mtls, verify=mtls.bundle, client_cert=mtls.client_cert)


def test_a_client_certificate_from_another_ca_is_refused(mtls):
    assert (
        _handshakes(mtls, verify=mtls.bundle, client_cert=mtls.stranger_cert) is False
    )


def test_a_client_certificate_keeps_the_default_trust(mtls, monkeypatch):
    # `verify=True` with a client certificate is docker-py's default trust,
    # which honours `REQUESTS_CA_BUNDLE`; without it the server is unknown.
    assert _handshakes(mtls, client_cert=mtls.client_cert) is False
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", mtls.bundle)
    assert _handshakes(mtls, client_cert=mtls.client_cert)


@_insecure
def test_a_client_certificate_with_verify_false(mtls):
    assert _handshakes(mtls, verify=False, client_cert=mtls.client_cert)


def test_one_pem_holding_both_is_passed_twice(mtls, tmp_path):
    cert, key = mtls.client_cert
    combined = tmp_path / "combined.pem"
    with open(cert, "rb") as first, open(key, "rb") as second:
        combined.write_bytes(first.read() + second.read())
    assert _handshakes(mtls, verify=mtls.bundle, client_cert=(combined, combined))


@pytest.mark.parametrize("which", (0, 1))
def test_a_missing_client_file_names_its_path_before_connecting(mtls, tmp_path, which):
    pair = list(mtls.client_cert)
    pair[which] = str(tmp_path / "absent.pem")
    before = len(mtls.requests)
    host = ContainerHost(
        ContainerConfig(
            "app", engine_url=mtls.url, verify=mtls.bundle, client_cert=tuple(pair)
        )
    )
    with pytest.raises(FileNotFoundError, match="absent.pem") as raised:
        host.client
    assert ("certificate", "key")[which] in str(raised.value)
    assert len(mtls.requests) == before


@pytest.mark.parametrize(
    "options",
    (
        {},
        {"engine_url": "tcp://127.0.0.1:2376"},
        {"engine_url": "unix:///var/run/docker.sock"},
        {"engine_url": "https://h:2376", "client_factory": lambda **kw: None},
    ),
)
def test_client_cert_needs_an_https_engine_the_config_builds(options):
    with pytest.raises(ValueError, match="client_cert"):
        ContainerConfig("app", client_cert=("c.pem", "k.pem"), **options)


@pytest.mark.parametrize(
    "value, error",
    (
        ("both.pem", TypeError),
        (("only.pem",), TypeError),
        (("c.pem", "k.pem", "x.pem"), TypeError),
        (("c.pem", b"k.pem"), TypeError),
        (("c.pem", ""), ValueError),
    ),
)
def test_client_cert_must_be_a_pair_of_paths(value, error):
    with pytest.raises(error, match="client_cert"):
        ContainerConfig("app", engine_url="https://h:2376", client_cert=value)


def test_client_cert_is_stored_as_str_and_is_keyword_only(tmp_path):
    config = ContainerConfig(
        "app", engine_url="https://h:2376", client_cert=[tmp_path / "c", "k"]
    )
    assert config.client_cert == (str(tmp_path / "c"), "k")
    with pytest.raises(TypeError):
        ContainerConfig("app", "https://h:2376", True, client_cert=("c", "k"))


def test_client_cert_is_never_shown():
    config = ContainerConfig(
        "app",
        engine_url="https://h:2376",
        client_cert=("/secret/c.pem", "/secret/k.pem"),
    )
    assert "secret" not in repr(config)
    assert "secret" not in config.connection_uri
    again = HostConfig(config.connection_uri)
    assert again.client_cert is None


def test_client_cert_is_a_credential_beside_a_uri():
    config = HostConfig(
        "docker://app?engine_url=https%3A%2F%2Fh%3A2376", client_cert=("c.pem", "k.pem")
    )
    assert config.client_cert == ("c.pem", "k.pem")
    with pytest.raises(ValueError, match="client_cert"):
        HostConfig("docker://app", client_cert=("c.pem", "k.pem"))
