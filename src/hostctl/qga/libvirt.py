"""Libvirt-backed QEMU Guest Agent transport."""

from __future__ import annotations

import itertools
import json
import math
import typing

from ._common import (
    QgaProtocolError,
    QgaTimeoutError,
    _QgaFramedSession,
)


def normalize_libvirt_error(error: BaseException) -> BaseException:
    """Normalize common libvirt/QGA failures without importing libvirt eagerly."""
    if not isinstance(error, Exception):
        return error
    message = str(error)
    folded = message.casefold()
    if any(value in folded for value in ("permission denied", "access denied")):
        return PermissionError(message)
    if any(
        value in folded
        for value in ("domain not found", "no domain with matching name")
    ):
        return FileNotFoundError(message)
    if "timed out" in folded or "timeout" in folded:
        return QgaTimeoutError(message)
    return ConnectionError(message)


class LibvirtGuestAgentTransport:
    """Issue QGA requests through ``virDomainQemuAgentCommand``."""

    def __init__(
        self,
        domain: str,
        *,
        connection_uri: typing.Optional[str] = None,
        timeout: float = 10.0,
        connect_factory: typing.Optional[
            typing.Callable[[typing.Optional[str]], object]
        ] = None,
        command_factory: typing.Optional[
            typing.Callable[[object, str, int, int], str]
        ] = None,
    ) -> None:
        if not domain:
            raise ValueError("libvirt domain must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.domain_name = domain
        self.connection_uri = connection_uri
        self.timeout = float(timeout)
        self._connect_factory = connect_factory
        self._command_factory = command_factory
        self._connection: typing.Optional[object] = None
        self._domain: typing.Optional[object] = None
        self._ids = itertools.count(1)

    def __enter__(self) -> LibvirtGuestAgentTransport:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: object,
    ) -> bool:
        self.close()
        return False

    def connect(self) -> None:
        if self._domain is not None:
            return
        connect_factory = self._connect_factory
        command_factory = self._command_factory
        if connect_factory is None or command_factory is None:
            try:
                import libvirt
                import libvirt_qemu
            except ImportError as exc:
                raise ImportError(
                    "libvirt QGA support requires the 'qemu-libvirt' extra"
                ) from exc
            connect_factory = connect_factory or libvirt.open
            command_factory = command_factory or libvirt_qemu.qemuAgentCommand
        connection = None
        try:
            connection = connect_factory(self.connection_uri)
            if connection is None:
                raise ConnectionError("libvirt returned no connection")
            domain = connection.lookupByName(self.domain_name)
            if not domain.isActive():
                close = getattr(connection, "close", None)
                if close is not None:
                    close()
                raise ConnectionError(
                    f"libvirt domain {self.domain_name!r} is not active"
                )
        except (ConnectionError, FileNotFoundError, PermissionError):
            if connection is not None:
                try:
                    close = getattr(connection, "close", None)
                    if close is not None:
                        close()
                except Exception:
                    pass
            raise
        except Exception as exc:
            try:
                if connection is not None:
                    close = getattr(connection, "close", None)
                    if close is not None:
                        close()
            except Exception:
                pass
            normalized = normalize_libvirt_error(exc)
            if normalized is exc:
                raise
            raise normalized from exc
        self._connect_factory = connect_factory
        self._command_factory = command_factory
        self._connection = connection
        self._domain = domain

    def close(self) -> None:
        connection, self._connection = self._connection, None
        self._domain = None
        if connection is not None:
            close = getattr(connection, "close", None)
            if close is not None:
                close()

    def execute(
        self,
        request: typing.Mapping[str, object],
        timeout: typing.Optional[float] = None,
    ) -> object:
        command = request.get("execute")
        if not isinstance(command, str) or not command:
            raise ValueError("QGA request requires a non-empty 'execute' string")
        request_timeout = self.timeout if timeout is None else float(timeout)
        if request_timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.connect()
        request_id = next(self._ids)
        payload = dict(request)
        payload["id"] = request_id
        assert self._domain is not None
        assert self._command_factory is not None
        try:
            raw = self._command_factory(
                self._domain,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                max(1, math.ceil(request_timeout)),
                0,
            )
        except Exception as exc:
            normalized = normalize_libvirt_error(exc)
            if normalized is exc:
                raise
            raise normalized from exc
        try:
            reply = json.loads(raw)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QgaProtocolError("libvirt returned malformed QGA JSON") from exc
        if not isinstance(reply, dict):
            raise QgaProtocolError("QGA reply must be a JSON object")
        if reply.get("id") not in (None, request_id):
            raise QgaProtocolError("libvirt returned a mismatched QGA reply")
        return _QgaFramedSession._unwrap(reply)
