"""QGA direct-stream transport over a fake AsyncSSH connection."""

from __future__ import annotations

import json

import pytest

from hostctl.qga._common import QgaCommandError, QgaProtocolError
from hostctl.qga.ssh import SshUnixGuestAgentTransport


def _frame(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


class _Reader:
    def __init__(self):
        self.received = bytearray()

    async def read(self, size):
        if not self.received:
            return b""
        value = bytes(self.received[:size])
        del self.received[:size]
        return value


class _Writer:
    def __init__(self, reader, *, oversized=False, command_error=False):
        self.reader = reader
        self.oversized = oversized
        self.command_error = command_error
        self.sent = []
        self.closed = False

    def write(self, value):
        self.sent.append(value)
        request = json.loads(value.lstrip(b"\xff"))
        if request["execute"] == "guest-sync-delimited":
            token = request["arguments"]["id"]
            self.reader.received.extend(
                b"stale\xff" + _frame({"return": token, "id": request["id"]})
            )
        elif self.oversized:
            self.reader.received.extend(b"{" + b"x" * 128 + b"}\n")
        elif self.command_error:
            self.reader.received.extend(
                _frame(
                    {
                        "error": {
                            "class": "CommandDisabled",
                            "desc": "blocked",
                        },
                        "id": request["id"],
                    }
                )
            )
        else:
            self.reader.received.extend(_frame({"return": {"ok": True}, "id": -1}))
            self.reader.received.extend(
                _frame({"return": {"value": 9}, "id": request["id"]})
            )

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class _Connection:
    def __init__(self, **writer_options):
        self.reader = _Reader()
        self.writer = _Writer(self.reader, **writer_options)
        self.calls = []

    async def open_unix_connection(self, path, *, encoding=None):
        self.calls.append((path, encoding))
        return self.reader, self.writer


def test_ssh_unix_qga_synchronizes_correlates_and_preserves_connection():
    connection = _Connection()
    transport = SshUnixGuestAgentTransport(
        "/run/qemu-server/guest.qga", lambda: connection
    )

    assert transport.execute({"execute": "guest-ping"}) == {"value": 9}
    assert connection.calls == [("/run/qemu-server/guest.qga", None)]
    assert connection.writer.sent[0].startswith(b"\xff")
    transport.close()
    assert connection.writer.closed


def test_ssh_unix_qga_bounds_and_structured_errors():
    oversized = _Connection(oversized=True)
    transport = SshUnixGuestAgentTransport(
        "/run/qga.sock",
        lambda: oversized,
        max_reply_size=32,
    )
    with pytest.raises(QgaProtocolError, match="size"):
        transport.execute({"execute": "guest-info"})
    assert oversized.writer.closed

    failed = _Connection(command_error=True)
    transport = SshUnixGuestAgentTransport("/run/qga.sock", lambda: failed)
    with pytest.raises(QgaCommandError) as raised:
        transport.execute({"execute": "guest-file-open"})
    assert raised.value.error_class == "CommandDisabled"


def test_ssh_unix_qga_rejects_invalid_requests_and_settings():
    connection = _Connection()
    with pytest.raises(ValueError, match="path"):
        SshUnixGuestAgentTransport("", lambda: connection)
    with pytest.raises(ValueError, match="timeout"):
        SshUnixGuestAgentTransport("/run/qga.sock", lambda: connection, timeout=0)
    transport = SshUnixGuestAgentTransport("/run/qga.sock", lambda: connection)
    with pytest.raises(ValueError, match="execute"):
        transport.execute({})
