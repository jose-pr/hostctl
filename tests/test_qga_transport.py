"""Focused tests for direct-socket and libvirt QGA transports."""

from __future__ import annotations

import json
import socket

import pytest

from hostctl.executor._qga import (
    LibvirtGuestAgentTransport,
    QgaCommandError,
    QgaProtocolError,
    QgaTimeoutError,
    UnixSocketGuestAgentTransport,
)


class _Socket:
    def __init__(self, *, timeout_command=False, oversized=False):
        self.connected = None
        self.closed = False
        self.sent = []
        self.received = bytearray()
        self.timeout_command = timeout_command
        self.oversized = oversized

    def settimeout(self, value):
        self.timeout = value

    def connect(self, path):
        self.connected = path

    def sendall(self, data):
        self.sent.append(data)
        request = json.loads(data.lstrip(b"\xff"))
        if request["execute"] == "guest-sync-delimited":
            token = request["arguments"]["id"]
            reply = {"return": token, "id": request["id"]}
            self.received.extend(b"stale partial\xff" + _frame(reply))
            return
        if self.timeout_command:
            return
        if self.oversized:
            self.received.extend(b"{" + b"x" * 128 + b"}\n")
            return
        self.received.extend(_frame({"return": {"ignored": True}, "id": -1}))
        self.received.extend(_frame({"return": {"value": 7}, "id": request["id"]}))

    def recv(self, size):
        if self.timeout_command and len(self.sent) > 1:
            raise socket.timeout("late")
        if not self.received:
            return b""
        result = bytes(self.received[:size])
        del self.received[:size]
        return result

    def close(self):
        self.closed = True


def _frame(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def test_unix_qga_synchronizes_and_correlates_requests():
    stream = _Socket()
    transport = UnixSocketGuestAgentTransport(
        "/run/qga.sock", socket_factory=lambda *args: stream
    )

    assert transport.execute({"execute": "example"}) == {"value": 7}
    assert stream.connected == "/run/qga.sock"
    assert stream.sent[0].startswith(b"\xff")
    assert b"guest-sync-delimited" in stream.sent[0]


def test_unix_qga_timeout_closes_and_resynchronizes_next_connection():
    first = _Socket(timeout_command=True)
    second = _Socket()
    streams = iter((first, second))
    transport = UnixSocketGuestAgentTransport(
        "/run/qga.sock", socket_factory=lambda *args: next(streams)
    )

    with pytest.raises(QgaTimeoutError):
        transport.execute({"execute": "slow"}, timeout=0.1)
    assert first.closed
    assert transport.execute({"execute": "retry"}) == {"value": 7}
    assert b"guest-sync-delimited" in second.sent[0]


def test_unix_qga_bounds_and_structured_errors():
    oversized = UnixSocketGuestAgentTransport(
        "/run/qga.sock",
        max_reply_size=32,
        socket_factory=lambda *args: _Socket(oversized=True),
    )
    with pytest.raises(QgaProtocolError, match="size"):
        oversized.execute({"execute": "large"})

    stream = _Socket()
    transport = UnixSocketGuestAgentTransport(
        "/run/qga.sock", socket_factory=lambda *args: stream
    )
    transport.connect()
    stream.received.extend(
        _frame(
            {
                "error": {"class": "CommandDisabled", "desc": "blocked"},
                "id": 2,
            }
        )
    )
    with pytest.raises(QgaCommandError) as raised:
        transport.execute({"execute": "blocked"})
    assert raised.value.error_class == "CommandDisabled"


class _Domain:
    def __init__(self, active=True):
        self.active = active

    def isActive(self):
        return self.active


class _Connection:
    def __init__(self, domain=None):
        self.domain = domain or _Domain()
        self.requested = []
        self.closed = False

    def lookupByName(self, name):
        self.requested.append(name)
        return self.domain

    def close(self):
        self.closed = True


def test_libvirt_transport_validates_domain_and_unwraps_reply():
    connection = _Connection()
    calls = []

    def command(domain, payload, timeout, flags):
        request = json.loads(payload)
        calls.append((domain, request, timeout, flags))
        return json.dumps({"return": {"ok": True}, "id": request["id"]})

    transport = LibvirtGuestAgentTransport(
        "guest",
        connection_uri="qemu:///system",
        connect_factory=lambda uri: connection,
        command_factory=command,
    )

    assert transport.execute({"execute": "guest-ping"}) == {"ok": True}
    assert connection.requested == ["guest"]
    assert calls[0][2:] == (10, 0)
    transport.close()
    assert connection.closed


def test_libvirt_transport_rejects_inactive_and_command_error():
    transport = LibvirtGuestAgentTransport(
        "stopped",
        connect_factory=lambda uri: _Connection(_Domain(active=False)),
        command_factory=lambda *args: "{}",
    )
    with pytest.raises(ConnectionError, match="not active"):
        transport.connect()

    connection = _Connection()

    def command(domain, payload, timeout, flags):
        request = json.loads(payload)
        return json.dumps(
            {
                "error": {"class": "GuestAgentNotAvailable", "desc": "offline"},
                "id": request["id"],
            }
        )

    transport = LibvirtGuestAgentTransport(
        "guest",
        connect_factory=lambda uri: connection,
        command_factory=command,
    )
    with pytest.raises(QgaCommandError, match="offline"):
        transport.execute({"execute": "guest-ping"})
