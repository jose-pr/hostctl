"""What `verify=True` trusts on the transports that speak HTTPS through `requests`.

The operating system's trust, checked by the platform's own verifier through
`truststore` on Python 3.10+ (else the store as `ssl.create_default_context()`
loads it), unless `REQUESTS_CA_BUNDLE` or `CURL_CA_BUNDLE` names a bundle,
which is then the only trust. certifi is never consulted. Each HTTP library
builds its own `requests` session, so the decision is installed on the adapter
that session mounts for `https://`; nothing else in the process is changed.
"""

from __future__ import annotations

import os
import ssl
import typing

#: The variables `requests` honours, in its order; the first non-empty one wins.
_ENV_BUNDLES = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def _os_trust() -> ssl.SSLContext:
    """A verifying client context that trusts what the operating system trusts.

    With `truststore`, the platform verifier decides: Windows CryptoAPI, which
    fetches a public root it has not stored yet, the macOS keychain, OpenSSL
    with the system's CA paths elsewhere. Without it (Python 3.9, or not
    installed), OpenSSL checks against the store as `ssl` loads it.
    """
    try:
        import truststore
    except ImportError:
        return ssl.create_default_context()
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _default_context() -> ssl.SSLContext:
    """The context `verify=True` checks with: an environment bundle, else the OS store.

    A bundle replaces the store rather than adding to it, as it replaces
    certifi in `requests`. A file or an OpenSSL hashed directory.
    """
    for name in _ENV_BUNDLES:
        path = os.environ.get(name)
        if not path:
            continue
        if os.path.isdir(path):
            return ssl.create_default_context(capath=path)
        if not os.path.isfile(path):
            # `ssl`'s own error names neither the path nor where it came from.
            raise FileNotFoundError(f"CA bundle from {name} not found: {path}")
        return ssl.create_default_context(cafile=path)
    return _os_trust()


def _trust_adapter(
    adapter: typing.Any, context: typing.Optional[ssl.SSLContext] = None
) -> None:
    """Make a `requests` `HTTPAdapter` verify with exactly `context`.

    `context` defaults to `_default_context()`. The adapter is changed in
    place, so a library's own adapter keeps its retries and pool hooks. An
    adapter already trusted is left alone: its open connections, and any
    authentication bound to them, belong to the context they were made with.
    """
    if getattr(adapter, "_hostctl_trust", None) is not None:
        return
    if context is None:
        context = _default_context()
    adapter._hostctl_trust = context
    adapter.poolmanager.connection_pool_kw["ssl_context"] = context
    for manager in getattr(adapter, "proxy_manager", {}).values():
        manager.connection_pool_kw["ssl_context"] = context

    cert_verify = adapter.cert_verify
    proxy_manager_for = adapter.proxy_manager_for

    def _cert_verify(conn: typing.Any, url: str, verify: object, cert: object) -> None:
        # `requests` points a verifying pool at a CA file (certifi, or the
        # `verify` path) and urllib3 loads that file INTO the pool's context;
        # the False branch sets none, and the check is switched back on here.
        cert_verify(conn, url, False, cert)
        if verify is not False and url.lower().startswith("https"):
            conn.cert_reqs = "CERT_REQUIRED"

    def _proxy_manager_for(proxy: str, **proxy_kwargs: object) -> object:
        proxy_kwargs["ssl_context"] = context
        return proxy_manager_for(proxy, **proxy_kwargs)

    adapter.cert_verify = _cert_verify
    adapter.proxy_manager_for = _proxy_manager_for


def _trust_session(session: typing.Any) -> None:
    """Install `_default_context()` on the adapter `session` mounts for `https://`."""
    _trust_adapter(session.get_adapter("https://"))
