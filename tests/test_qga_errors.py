"""Guest-agent failures keep one vocabulary across every QGA transport."""

from __future__ import annotations

import asyncio
import errno
import json

import pytest

from hostctl.executor._qga import (
    QgaCommandError,
    QgaTimeoutError,
    SshUnixGuestAgentTransport,
    UnixSocketGuestAgentTransport,
    guest_oserror,
    normalize_libvirt_error,
)


def _frame(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def test_a_guest_path_that_spells_an_error_is_not_classified_as_one():
    """qemu-ga writes "<context>: <strerror>" and the context holds the path.

    Classifying the whole message meant a file named `notfound.log` was
    reported missing whatever had actually happened to it -- including a
    permission failure, which then looked to `staged_open` like a file it
    could create.
    """
    denied = QgaCommandError(
        "GenericError",
        "Failed to open file '/var/log/notfound.log': Permission denied",
    )
    mapped = guest_oserror(denied, "/var/log/notfound.log")

    assert isinstance(mapped, PermissionError)
    assert mapped.errno == errno.EACCES
    assert mapped.filename == "/var/log/notfound.log"


def test_a_mapped_guest_error_carries_an_errno_and_a_filename():
    missing = QgaCommandError(
        "GenericError",
        "Failed to open file '/tmp/absent': No such file or directory",
    )
    mapped = guest_oserror(missing, "/tmp/absent")

    assert isinstance(mapped, FileNotFoundError)
    assert mapped.errno == errno.ENOENT
    assert mapped.filename == "/tmp/absent"


class _LibvirtError(Exception):
    """A stand-in with libvirt's own `get_error_code` accessor."""

    def __init__(self, message, code):
        super().__init__(message)
        self._code = code

    def get_error_code(self):
        return self._code


def test_libvirt_relays_a_guest_error_as_a_guest_error():
    """libvirt raises on the guest's behalf; the guest still answered.

    Reported as `ConnectionError`, an ordinary "no such file" read as a dead
    link -- so the two transport families disagreed about what a guest error
    is, and the path layer's classifier never saw it.
    """
    relayed = _LibvirtError(
        "internal error: unable to execute QEMU agent command "
        "'guest-file-open': Failed to open file '/tmp/x': "
        "No such file or directory",
        1,
    )

    normalized = normalize_libvirt_error(relayed)

    assert isinstance(normalized, QgaCommandError)
    assert normalized.description.endswith("No such file or directory")
    assert isinstance(
        guest_oserror(normalized, "/tmp/x"),
        FileNotFoundError,
    )


def test_a_libvirt_failure_is_classified_by_code_not_by_the_guest_path():
    """VIR_ERR_INTERNAL_ERROR (1) is a connection failure, not a timeout.

    The substring reading turned any message mentioning a path like
    `/srv/timeout/socket` into a `TimeoutError`, which callers retry.
    """
    internal = _LibvirtError("internal error: /srv/timeout/socket is gone", 1)
    assert type(normalize_libvirt_error(internal)) is ConnectionError

    timed_out = _LibvirtError("operation aborted", 68)  # VIR_ERR_OPERATION_TIMEOUT
    assert isinstance(normalize_libvirt_error(timed_out), QgaTimeoutError)

    unresponsive = _LibvirtError("agent gone", 86)  # VIR_ERR_AGENT_UNRESPONSIVE
    assert isinstance(normalize_libvirt_error(unresponsive), QgaTimeoutError)


class _ErrorSocket:
    """A socket that synchronizes and then answers every request with an error."""

    def __init__(self):
        self.connects = 0
        self.syncs = 0
        self.received = bytearray()
        self.closed = 0

    def settimeout(self, value):
        pass

    def connect(self, path):
        self.connects += 1

    def sendall(self, data):
        request = json.loads(data.lstrip(b"\xff"))
        if request["execute"] == "guest-sync-delimited":
            self.syncs += 1
            self.received.extend(
                b"\xff"
                + _frame({"return": request["arguments"]["id"], "id": request["id"]})
            )
            return
        self.received.extend(
            _frame(
                {
                    "error": {"class": "GenericError", "desc": "no such file"},
                    "id": request["id"],
                }
            )
        )

    def recv(self, size):
        if not self.received:
            return b""
        value = bytes(self.received[:size])
        del self.received[:size]
        return value

    def close(self):
        self.closed += 1


def test_a_guest_error_reply_keeps_the_connection():
    """The guest answered: the stream is at a frame boundary.

    Tearing it down made every "no such file" cost a reconnect and a fresh
    `guest-sync-delimited` -- three round trips for an `exists()` that
    returns False.
    """
    socket_ = _ErrorSocket()
    transport = UnixSocketGuestAgentTransport(
        "/run/qga.sock", socket_factory=lambda *a, **k: socket_
    )

    for _ in range(3):
        with pytest.raises(QgaCommandError):
            transport.execute({"execute": "guest-file-open"})

    assert socket_.connects == 1
    assert socket_.syncs == 1
    assert socket_.closed == 0


class _StaleSocket(_ErrorSocket):
    """A socket whose buffer still holds an abandoned session's reply."""

    def sendall(self, data):
        request = json.loads(data.lstrip(b"\xff"))
        if request["execute"] == "guest-sync-delimited":
            self.syncs += 1
            # A stale delimiter, then a stale sync answer with a token that
            # is not ours, and only then the real one.
            self.received.extend(
                b"noise\xff"
                + _frame({"return": 1234, "id": request["id"]})
                + b"\xff"
                + _frame({"return": request["arguments"]["id"], "id": request["id"]})
            )
            return
        super().sendall(data)


def test_connect_resynchronizes_past_an_abandoned_session():
    """One discard/read pair gave up where the protocol says keep scanning."""
    socket_ = _StaleSocket()
    transport = UnixSocketGuestAgentTransport(
        "/run/qga.sock", socket_factory=lambda *a, **k: socket_
    )

    with pytest.raises(QgaCommandError):
        transport.execute({"execute": "guest-file-open"})

    assert socket_.connects == 1


class _SlowConnection:
    """An SSH connection whose `open_unix_connection` never completes."""

    async def open_unix_connection(self, path, *, encoding=None):
        await asyncio.sleep(30)
        raise AssertionError("unreachable")


def test_an_ssh_connect_deadline_is_a_qga_timeout_on_every_python():
    """A guard, not a repair: this already held, by a longer route.

    On the 3.9 floor `asyncio.TimeoutError` is a distinct class from the
    builtin, and the connect leg carried no timeout branch of its own --
    but measured on 3.9.13, `normalize_asyncssh_error` maps it to
    `subprocess.TimeoutExpired` and `_normalize_ssh_error` then answers
    `QgaTimeoutError`, so the symptom never reached a caller. The three
    legs now share one ladder that says so directly; this test pins the
    behaviour to the contract rather than to that chain.
    """
    transport = SshUnixGuestAgentTransport(
        "/run/qga.sock", lambda: _SlowConnection(), timeout=0.05
    )

    with pytest.raises(QgaTimeoutError):
        transport.execute({"execute": "guest-ping"})
