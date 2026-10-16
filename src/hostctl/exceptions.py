"""Every exception hostctl raises of its own.

`HostctlError` is the base. Each category also inherits the builtin a caller
would already catch, so `except ConnectionError` and `except TimeoutError`
keep working without naming a hostctl class. A caller's bad argument is a
plain `ValueError` or `TypeError`, never a `HostctlError`.
"""

from __future__ import annotations

import typing

__all__ = [
    "HostctlError",
    "TransportError",
    "OperationNotStartedError",
    "QGADisconnectedError",
    "ProtocolError",
    "QGAProtocolError",
    "ConsoleProtocolError",
    "QGATimeoutError",
    "QGACommandError",
    "BundleError",
]


class HostctlError(Exception):
    """Base of every exception hostctl raises of its own."""


class TransportError(HostctlError, ConnectionError):
    """The connection to the target failed or was never made."""


class OperationNotStartedError(TransportError):
    """A provider declined before anything was sent to the target.

    Nothing ran, so retrying the operation elsewhere is safe; this is the only
    failure ordered provider fallback moves past. `cause` is the transport
    error that made the provider decline, when there was one.
    """

    def __init__(
        self,
        reason: str = "operation was not started",
        *,
        cause: typing.Optional[Exception] = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cause = cause


class QGADisconnectedError(TransportError):
    """The guest-agent transport disconnected before producing a reply."""


class ProtocolError(HostctlError, ConnectionError):
    """The peer answered, but not in the protocol's expected shape."""


class QGAProtocolError(ProtocolError):
    """The guest agent returned malformed or inconsistent protocol data."""


class ConsoleProtocolError(ProtocolError):
    """The console did not complete the expected protocol exchange."""


class QGATimeoutError(HostctlError, TimeoutError):
    """A guest-agent request did not complete before its deadline."""


class QGACommandError(HostctlError):
    """The guest agent refused a command with a structured error.

    `error_class` and `description` are the agent's own fields; `data` holds
    any extra detail it sent.
    """

    def __init__(
        self,
        error_class: str,
        description: str,
        *,
        data: typing.Optional[typing.Mapping[str, object]] = None,
    ) -> None:
        super().__init__(f"{error_class}: {description}")
        self.error_class = error_class
        self.description = description
        self.data = dict(data or {})


class BundleError(HostctlError, RuntimeError):
    """A deploy bundle cannot be built correctly from what is installed here.

    Raised before anything is sent to the target: a distribution with a
    compiled extension or nothing importable, a missing ignore file, a source
    tree without an importable package.
    """
