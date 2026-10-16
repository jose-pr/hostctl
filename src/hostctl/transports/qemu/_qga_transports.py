"""Guest-agent transports: libvirt, a Unix socket, and a Unix socket over SSH."""

from __future__ import annotations

import asyncio

import contextlib


import itertools

import json

import math

import re


import socket

import subprocess

import threading


import typing

from ...exceptions import (
    HostctlError,
    QGACommandError,
    QGADisconnectedError,
    QGAProtocolError,
    QGATimeoutError,
)

from ._qga import _QGAFramedSession, _QGARequestNotSerializable

#: libvirt's own error codes, so a `libvirtError` can be classified without
#: importing libvirt into a process that may not have it. These are stable
#: API constants (`include/libvirt/virterror.h`).
_LIBVIRT_NO_DOMAIN = 42

_LIBVIRT_AUTH_FAILED = 45

_LIBVIRT_OPERATION_TIMEOUT = 68

_LIBVIRT_AGENT_UNRESPONSIVE = 86

_LIBVIRT_ACCESS_DENIED = 87


#: How libvirt relays a guest-agent error reply: it raises, with the guest's
#: own description wrapped in a sentence of its own.
_AGENT_COMMAND_ERROR = re.compile(
    r"unable to execute QEMU agent command '[^']*':\s*(?P<desc>.+)",
    re.DOTALL,
)


def normalize_libvirt_error(error: BaseException) -> BaseException:
    """Normalize common libvirt/QGA failures without importing libvirt eagerly."""
    if not isinstance(error, Exception):
        return error
    message = str(error)
    relayed = _AGENT_COMMAND_ERROR.search(message)
    if relayed is not None:
        # The guest answered and libvirt raised on its behalf. Reported as
        # `ConnectionError`, an ordinary "no such file" looked like a dead
        # link: the caller retried, `_qga_error` never saw it, and the two
        # transport families disagreed about what a guest error even is.
        return QGACommandError("GenericError", relayed.group("desc").strip())
    code = None
    get_error_code = getattr(error, "get_error_code", None)
    if callable(get_error_code):
        try:
            code = get_error_code()
        except Exception:
            code = None
    if isinstance(code, int):
        if code in (_LIBVIRT_OPERATION_TIMEOUT, _LIBVIRT_AGENT_UNRESPONSIVE):
            return QGATimeoutError(message)
        if code in (_LIBVIRT_ACCESS_DENIED, _LIBVIRT_AUTH_FAILED):
            return PermissionError(message)
        if code == _LIBVIRT_NO_DOMAIN:
            return FileNotFoundError(message)
        return ConnectionError(message)
    # No code to read -- a plain exception from a connect factory, or a
    # message relayed by something other than libvirt itself. The text is
    # all there is, so keep the substring reading for that case only: it is
    # what made a guest path containing "timeout" a TimeoutError.
    folded = message.casefold()
    if any(value in folded for value in ("permission denied", "access denied")):
        return PermissionError(message)
    if any(
        value in folded
        for value in ("domain not found", "no domain with matching name")
    ):
        return FileNotFoundError(message)
    if "timed out" in folded or "timeout" in folded:
        return QGATimeoutError(message)
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
        # `connect()` is reached from every `execute()`, so two threads
        # using one transport each opened a libvirt connection and one was
        # then overwritten still open.
        self._lock = threading.RLock()

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
        with self._lock:
            self._connect_locked()

    def _connect_locked(self) -> None:
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
                    "libvirt QGA support requires the 'qemu-libvirt' extra: "
                    "pip install hostctl[qemu-libvirt]"
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
            # A transport-level failure means the cached connection is dead --
            # libvirtd restarted, the socket went away, the domain vanished.
            # Keeping the handle made one restart fatal for the life of the
            # process, while the framed transports reconnect transparently on
            # the next request. A QGACommandError is the guest answering and
            # keeps the connection.
            if isinstance(
                normalized, (ConnectionError, FileNotFoundError, PermissionError)
            ):
                try:
                    self.close()
                except Exception:
                    pass
            if normalized is exc:
                raise
            raise normalized from exc
        try:
            reply = json.loads(raw)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QGAProtocolError("libvirt returned malformed QGA JSON") from exc
        if not isinstance(reply, dict):
            raise QGAProtocolError("QGA reply must be a JSON object")
        if reply.get("id") not in (None, request_id):
            raise QGAProtocolError("libvirt returned a mismatched QGA reply")
        return _QGAFramedSession._unwrap(reply)


class UnixSocketGuestAgentTransport(_QGAFramedSession):
    """Persistent QGA connection over a Unix-domain socket."""

    def __init__(
        self,
        path: str,
        *,
        timeout: float = 10.0,
        max_reply_size: int = 8 * 1024 * 1024,
        socket_factory: typing.Callable[..., socket.socket] = socket.socket,
    ) -> None:
        if not path:
            raise ValueError("QGA socket path must not be empty")
        self.path = path
        self._socket_factory = socket_factory
        self._uses_system_socket = socket_factory is socket.socket
        self._socket: typing.Optional[socket.socket] = None
        super().__init__(timeout=timeout, max_reply_size=max_reply_size)

    def __enter__(self) -> UnixSocketGuestAgentTransport:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False

    def _connect_raw(self, deadline: float) -> None:
        if not hasattr(socket, "AF_UNIX") and self._uses_system_socket:
            raise NotImplementedError(
                "direct QGA sockets require Unix-domain socket support"
            )
        family = getattr(socket, "AF_UNIX", 1)
        connection = self._socket_factory(family, socket.SOCK_STREAM)
        self._socket = connection
        try:
            connection.settimeout(self._remaining(deadline))
            connection.connect(self.path)
        except socket.timeout as exc:
            self._drop_socket(connection)
            raise QGATimeoutError(
                f"timed out connecting to the QGA socket {self.path}"
            ) from exc
        except OSError as exc:
            # A missing or refused socket is a *transport* failure. Raised as
            # the bare FileNotFoundError it arrives as, it was indistinguishable
            # from "the guest file does not exist": `_qga_error` matched the
            # message and returned FileNotFoundError for the guest path, and
            # `staged_open` read that as "'a' creates what is missing", staged
            # an empty buffer, and truncated the guest file on close.
            self._drop_socket(connection)
            raise QGADisconnectedError(
                f"cannot reach the QGA socket {self.path}"
            ) from exc
        except Exception:
            self._drop_socket(connection)
            raise

    def _drop_socket(self, connection: "socket.socket") -> None:
        try:
            connection.close()
        finally:
            self._socket = None

    def _send_raw(self, data: bytes, deadline: float) -> None:
        connection = self._require_socket()
        try:
            connection.settimeout(self._remaining(deadline))
            connection.sendall(data)
        except socket.timeout as exc:
            raise QGATimeoutError("timed out sending a QGA request") from exc
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            raise QGADisconnectedError("QGA disconnected while sending") from exc

    def _recv_raw(self, size: int, deadline: float) -> bytes:
        connection = self._require_socket()
        try:
            connection.settimeout(self._remaining(deadline))
            return connection.recv(size)
        except socket.timeout as exc:
            raise QGATimeoutError("timed out waiting for a QGA reply") from exc
        except (ConnectionResetError, ConnectionAbortedError) as exc:
            raise QGADisconnectedError("QGA disconnected while receiving") from exc

    def _close_raw(self) -> None:
        connection, self._socket = self._socket, None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise QGADisconnectedError("QGA transport is not connected")
        return self._socket


class _Reader(typing.Protocol):
    def read(self, size: int) -> typing.Awaitable[bytes]: ...


class _Writer(typing.Protocol):
    def write(self, value: bytes) -> None: ...

    def drain(self) -> typing.Awaitable[None]: ...

    def close(self) -> None: ...


class _SSHConnection(typing.Protocol):
    def open_unix_connection(
        self, path: str, *, encoding: typing.Optional[str] = None
    ) -> typing.Awaitable[typing.Tuple[_Reader, _Writer]]: ...


async def _wait(awaitable: typing.Awaitable[object], timeout: float) -> object:
    return await asyncio.wait_for(awaitable, timeout)


#: How long a channel close may wait for the peer's acknowledgement.
#: Teardown runs while `_session_lock` is held and is reached from every
#: failed `execute()`, so an unbounded wait here makes `timeout=` meaningless
#: -- the deadline expires and the thread then blocks forever on a partitioned
#: link, taking every other user of the host with it.
_CLOSE_BUDGET = 5.0


async def _close_writer(writer: _Writer, timeout: float = _CLOSE_BUDGET) -> None:
    writer.close()
    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is not None:
        result = wait_closed()
        if hasattr(result, "__await__"):
            try:
                await asyncio.wait_for(result, timeout)
            except (asyncio.TimeoutError, TimeoutError):
                # `close()` has already been issued; the peer simply never
                # answered. Dropping the handle is the whole of what is left.
                pass


class SSHUnixGuestAgentTransport(_QGAFramedSession):
    """Persistent QGA stream opened on a remote Unix socket over SSH."""

    #: Seconds the teardown waits for the peer to acknowledge the close.
    close_timeout: float = _CLOSE_BUDGET

    def __init__(
        self,
        path: str,
        connection: typing.Callable[[], _SSHConnection],
        *,
        timeout: float = 10.0,
        max_reply_size: int = 8 * 1024 * 1024,
    ) -> None:
        if not path:
            raise ValueError("remote QGA socket path must not be empty")
        self.path = path
        self._connection = connection
        self._reader: typing.Optional[_Reader] = None
        self._writer: typing.Optional[_Writer] = None
        super().__init__(timeout=timeout, max_reply_size=max_reply_size)

    def __enter__(self) -> SSHUnixGuestAgentTransport:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False

    @contextlib.contextmanager
    def _failures(self, command: str, timed_out: str) -> typing.Iterator[None]:
        """One error ladder for every leg of this transport.

        Written out three times, the connect leg was missing the timeout
        branch -- and on the 3.9 floor asyncio.TimeoutError is a distinct
        class from the builtin, so a connect deadline escaped raw where a
        send or receive deadline raised QGATimeoutError. One condition, two
        exception types, decided by which Python was running.
        """
        try:
            yield
        except (HostctlError, _QGARequestNotSerializable):
            # Already in this vocabulary -- a deadline _remaining() noticed,
            # for instance. Re-wrapping would relabel it.
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise QGATimeoutError(timed_out) from exc
        except Exception as exc:
            normalized = self._normalize_ssh_error(exc, command)
            if normalized is exc:
                raise
            raise normalized from exc

    def _connect_raw(self, deadline: float) -> None:
        from ... import _async

        with self._failures("open QGA stream", "timed out opening the QGA stream"):
            opened = _async.async_to_sync(
                _wait(
                    self._connection().open_unix_connection(self.path, encoding=None),
                    self._remaining(deadline),
                )
            )
            self._reader, self._writer = typing.cast(
                typing.Tuple[_Reader, _Writer], opened
            )

    def _send_raw(self, data: bytes, deadline: float) -> None:
        from ... import _async

        writer = self._require_writer()

        async def send() -> None:
            writer.write(data)
            await asyncio.wait_for(writer.drain(), self._remaining(deadline))

        with self._failures("send QGA request", "timed out sending a QGA request"):
            _async.async_to_sync(send())

    def _recv_raw(self, size: int, deadline: float) -> bytes:
        from ... import _async

        with self._failures("receive QGA reply", "timed out waiting for a QGA reply"):
            chunk = _async.async_to_sync(
                _wait(self._require_reader().read(size), self._remaining(deadline))
            )
        if not isinstance(chunk, bytes):
            raise TypeError("SSH QGA stream returned non-byte data")
        return chunk

    def _close_raw(self) -> None:
        from ... import _async

        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            try:
                _async.async_to_sync(_close_writer(writer, self.close_timeout))
            except Exception:
                pass

    def _require_reader(self) -> _Reader:
        if self._reader is None:
            raise QGADisconnectedError("QGA transport is not connected")
        return self._reader

    def _require_writer(self) -> _Writer:
        if self._writer is None:
            raise QGADisconnectedError("QGA transport is not connected")
        return self._writer

    @staticmethod
    def _normalize_ssh_error(exc: Exception, command: str) -> Exception:
        from ... import _async

        normalized = _async.normalize_asyncssh_error(exc, command=command)
        if isinstance(normalized, subprocess.TimeoutExpired):
            return QGATimeoutError(str(normalized))
        if isinstance(normalized, ConnectionError):
            return QGADisconnectedError(str(normalized))
        return normalized
