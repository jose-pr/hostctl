"""Why a transport could not connect, as one line an operator can act on.

The text becomes the provider's decline reason, which is what
`SelectionStep.reason` and the host's fallback WARNING show.
"""

from __future__ import annotations

import asyncio
import errno
import socket
import ssl
import typing

from ._selector import ProviderSelector
from ..uri._parse import uri_host

#: errno values (POSIX and Winsock) of a host or network that cannot be reached.
_UNREACHABLE = frozenset(
    value
    for value in (
        getattr(errno, name, None)
        for name in (
            "EHOSTUNREACH",
            "ENETUNREACH",
            "EHOSTDOWN",
            "ENETDOWN",
            "WSAEHOSTUNREACH",
            "WSAENETUNREACH",
            "WSAEHOSTDOWN",
            "WSAENETDOWN",
        )
    )
    if value is not None
)

#: Longest detail kept from an error message, so the WARNING stays one line.
_DETAIL = 240


def _chain(exc: BaseException) -> typing.List[BaseException]:
    """`exc` and every error it wraps, outermost first.

    Libraries wrap differently: `raise ... from` and implicit context,
    `OperationNotStartedError.cause`, urllib3's `.reason`, and requests'
    exception arguments all hold the error that says what happened.
    """
    found: typing.List[BaseException] = []
    seen: typing.Set[int] = set()
    queue: typing.List[object] = [exc]
    while queue and len(found) < 32:
        item = queue.pop(0)
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        seen.add(id(item))
        found.append(item)
        queue += [
            getattr(item, "cause", None),
            getattr(item, "reason", None),
            item.__cause__,
            item.__context__,
            *getattr(item, "args", ()),
        ]
    return found


def _named(error: BaseException, *names: str) -> bool:
    """Whether a class in `error`'s MRO has one of `names` (no import needed)."""
    return any(kind.__name__ in names for kind in type(error).__mro__)


def _timed_out(error: BaseException) -> bool:
    # socket.timeout and asyncio.TimeoutError are distinct classes before 3.10/3.11.
    return isinstance(
        error, (TimeoutError, socket.timeout, asyncio.TimeoutError)
    ) or _named(error, "ConnectTimeout", "ConnectTimeoutError", "ReadTimeout")


_CATEGORIES: typing.Tuple[
    typing.Tuple[str, typing.Callable[[BaseException], bool]], ...
] = (
    ("host_key", lambda e: _named(e, "HostKeyNotVerifiable")),
    ("certificate", lambda e: isinstance(e, ssl.SSLCertVerificationError)),
    (
        "login",
        lambda e: isinstance(e, PermissionError)
        or _named(e, "PermissionDenied", "InvalidCredentialsError"),
    ),
    ("name", lambda e: isinstance(e, socket.gaierror)),
    ("refused", lambda e: isinstance(e, ConnectionRefusedError)),
    ("timeout", _timed_out),
    (
        "unreachable",
        lambda e: isinstance(e, OSError) and e.errno in _UNREACHABLE,
    ),
    ("closed", lambda e: isinstance(e, (ConnectionResetError, ConnectionAbortedError))),
)


def _detail(error: BaseException) -> str:
    text = " ".join(str(error).split())
    if len(text) > _DETAIL:
        text = text[: _DETAIL - 3] + "..."
    return ProviderSelector.redact(text)


def connect_failure(
    service: str,
    host: str,
    port: typing.Optional[int],
    exc: BaseException,
    *,
    trust: str,
) -> str:
    """The reason `service` could not reach `host`: what failed, then the error.

    `trust` is the remedy named when the host key or TLS certificate is not
    trusted. Credentials are redacted from the error text.
    """
    target = uri_host(host) if port is None else f"{uri_host(host)}:{port}"
    chain = _chain(exc)
    for category, matches in _CATEGORIES:
        error = next((item for item in chain if matches(item)), None)
        if error is not None:
            break
    else:
        category = "failed"
        error = next((item for item in chain if str(item)), exc)
    detail = _detail(error)
    if category == "host_key":
        return f"{service} host key of {target} is not trusted ({detail}); {trust}"
    if category == "certificate":
        return (
            f"{service} TLS certificate of {target} is not trusted ({detail}); {trust}"
        )
    if category == "login":
        return f"{service} login to {target} was refused ({detail})"
    if category == "name":
        return f"{service} host name {host} did not resolve ({detail})"
    if category == "refused":
        return f"{service} connection to {target} was refused ({detail})"
    if category == "timeout":
        suffix = f" ({detail})" if detail else ""
        return f"{service} connection to {target} timed out{suffix}"
    if category == "unreachable":
        return f"{service} host {target} is unreachable ({detail})"
    if category == "closed":
        return f"{service} connection to {target} was closed by the peer ({detail})"
    return f"{service} connection to {target} failed ({type(error).__name__}: {detail})"
