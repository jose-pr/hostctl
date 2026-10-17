"""A throwaway certificate authority and an HTTPS server on 127.0.0.1.

Tests of certificate verification need a server whose certificate no real
trust store accepts, and a CA bundle that does accept it -- built here with
`cryptography`, in memory and on disk, with no network involved.
"""

from __future__ import annotations

import datetime
import http.server
import ipaddress
import json
import os
import ssl
import threading
import typing

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


class ThrowawayCA:
    """A self-signed CA; `issue()` signs a server certificate for 127.0.0.1."""

    def __init__(self, common_name: str = "hostctl test CA") -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(_name(common_name))
            .issuer_name(_name(common_name))
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()),
                False,
            )
            .sign(self.key, hashes.SHA256())
        )

    @property
    def pem(self) -> bytes:
        return _pem(self.cert)

    def issue(self) -> typing.Tuple[bytes, bytes]:
        """A `(certificate chain PEM, key PEM)` for `localhost` and 127.0.0.1."""
        now = datetime.datetime.now(datetime.timezone.utc)
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (
            x509.CertificateBuilder()
            .subject_name(_name("localhost"))
            .issuer_name(self.cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    ]
                ),
                False,
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    self.key.public_key()
                ),
                False,
            )
            .sign(self.key, hashes.SHA256())
        )
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return _pem(cert) + self.pem, key_pem

    def write_bundle(self, path: "os.PathLike[str] | str") -> str:
        with open(path, "wb") as handle:
            handle.write(self.pem)
        return os.fspath(path)

    def write_hashed_dir(self, directory: "os.PathLike[str] | str") -> str:
        """An OpenSSL `-CApath` directory: the CA under its subject hash."""
        import subprocess

        directory = os.fspath(directory)
        os.makedirs(directory, exist_ok=True)
        pem_path = os.path.join(directory, "ca.pem")
        self.write_bundle(pem_path)
        # The subject hash OpenSSL looks up; computed through `ssl` itself so
        # no `openssl` binary is needed.
        name = _subject_hash(self.cert)
        os.replace(pem_path, os.path.join(directory, f"{name}.0"))
        del subprocess
        return directory


def _subject_hash(cert: x509.Certificate) -> str:
    """OpenSSL's `X509_NAME_hash` (the `-subject_hash` of `openssl x509`)."""
    import hashlib

    # The canonical encoding: each RDN's value lower-cased with whitespace
    # collapsed, re-encoded as UTF8String, then the SET sequence hashed with
    # SHA-1; the first four bytes, little-endian. Only one CN RDN is used here.
    value = " ".join(cert.subject.rfc4514_string().split("=", 1)[1].lower().split())
    encoded_value = _der(0x0C, value.encode())
    oid = _der(0x06, bytes([0x55, 0x04, 0x03]))
    attribute = _der(0x30, oid + encoded_value)
    rdn = _der(0x31, attribute)
    digest = hashlib.sha1(rdn).digest()
    return "%08x" % int.from_bytes(digest[:4], "little")


def _der(tag: int, body: bytes) -> bytes:
    length = len(body)
    if length < 0x80:
        return bytes([tag, length]) + body
    size = (length.bit_length() + 7) // 8
    return bytes([tag, 0x80 | size]) + length.to_bytes(size, "big") + body


class _Handler(http.server.BaseHTTPRequestHandler):
    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.requests.append((self.command, self.path))  # type: ignore[attr-defined]
        if self.command == "GET":
            # Enough of the Docker Engine's `/version` for docker-py's client.
            body = json.dumps({"ApiVersion": "1.44", "Version": "0"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = b""
            self.send_response(401)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    do_GET = _answer
    do_POST = _answer

    def log_message(self, format: str, *args: object) -> None:
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        # A refused handshake is the expected outcome of half the tests.
        pass


class TLSServer:
    """HTTPS on 127.0.0.1 with a certificate issued by `ca`.

    GET answers a minimal Docker `/version` (200), POST answers 401, so a
    client that completes the handshake gets an HTTP-level failure and one
    that refuses the certificate an SSL error. `requests` records each
    request that got past the handshake.
    """

    def __init__(self, ca: ThrowawayCA, directory: "os.PathLike[str] | str") -> None:
        chain, key = ca.issue()
        self._cert = os.path.join(os.fspath(directory), "server.pem")
        self._key = os.path.join(os.fspath(directory), "server.key")
        with open(self._cert, "wb") as handle:
            handle.write(chain)
        with open(self._key, "wb") as handle:
            handle.write(key)
        self.requests: typing.List[typing.Tuple[str, str]] = []

    def __enter__(self) -> "TLSServer":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self._cert, self._key)
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.requests = self.requests  # type: ignore[attr-defined]
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)


def os_store_holds(monkeypatch: typing.Any, bundle: str) -> None:
    """Make hostctl's OS-store loading return `bundle` as the whole store.

    Simulates a CA installed in the operating system's trust store without
    touching the real one.
    """
    from hostctl.host import _tls

    monkeypatch.setattr(
        _tls, "_os_trust", lambda: ssl.create_default_context(cafile=bundle)
    )


def certifi_holds(monkeypatch: typing.Any, bundle: str) -> None:
    """Make `requests`' own default trust -- certifi -- accept `bundle`'s CA.

    `requests` reads certifi through `DEFAULT_CA_BUNDLE_PATH`, and 2.32.3 and
    2.32.4 also through a context loaded at import; both are replaced.
    """
    import requests.adapters

    monkeypatch.setattr(requests.adapters, "DEFAULT_CA_BUNDLE_PATH", bundle)
    if getattr(requests.adapters, "_preloaded_ssl_context", None) is not None:
        monkeypatch.setattr(
            requests.adapters,
            "_preloaded_ssl_context",
            ssl.create_default_context(cafile=bundle),
        )
