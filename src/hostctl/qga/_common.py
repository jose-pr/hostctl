"""QEMU Guest Agent transport contracts and normalized errors."""

from __future__ import annotations

import typing


class QgaError(Exception):
    """Base class for QEMU Guest Agent failures."""


class QgaProtocolError(QgaError, ConnectionError):
    """The guest agent returned malformed or inconsistent protocol data."""


class QgaDisconnectedError(QgaError, ConnectionError):
    """The guest-agent transport disconnected before producing a reply."""


class QgaTimeoutError(QgaError, TimeoutError):
    """A guest-agent request did not complete before its deadline."""


class QgaCommandError(QgaError):
    """A structured QGA command error returned by the guest."""

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


@typing.runtime_checkable
class GuestAgentTransport(typing.Protocol):
    """Synchronous transport for one correlated QGA request."""

    def execute(
        self,
        request: typing.Mapping[str, object],
        timeout: typing.Optional[float] = None,
    ) -> object:
        """Return the unwrapped QGA ``return`` value."""

    def close(self) -> None:
        """Release transport resources."""
