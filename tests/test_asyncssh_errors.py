"""AsyncSSH errors exposed through the synchronous host contract."""

import subprocess

import pytest

asyncssh = pytest.importorskip("asyncssh")

from hostctl import _async


def test_asyncssh_authentication_error_normalizes_to_permission_error():
    result = _async.normalize_asyncssh_error(
        asyncssh.PermissionDenied("authentication failed")
    )

    assert isinstance(result, PermissionError)
    assert "authentication failed" in str(result)


@pytest.mark.parametrize(
    "error",
    (
        asyncssh.ConnectionLost("connection lost"),
        asyncssh.HostKeyNotVerifiable("host key rejected"),
        asyncssh.KeyExchangeFailed("key exchange failed"),
        asyncssh.ProtocolError("protocol failed"),
        asyncssh.ChannelOpenError(1, "channel rejected"),
    ),
)
def test_asyncssh_transport_errors_normalize_to_connection_error(error):
    result = _async.normalize_asyncssh_error(error)

    assert isinstance(result, ConnectionError)
    assert str(result)


def test_asyncssh_process_error_normalizes_to_called_process_error():
    error = asyncssh.ProcessError(
        None,
        "false",
        None,
        7,
        None,
        7,
        b"out",
        b"err",
    )

    result = _async.normalize_asyncssh_error(error)

    assert isinstance(result, subprocess.CalledProcessError)
    assert result.returncode == 7
    assert result.cmd == "false"
    assert result.stdout == b"out"
    assert result.stderr == b"err"


def test_asyncssh_timeout_normalizes_to_timeout_expired_with_output():
    error = asyncssh.TimeoutError(
        None,
        "sleep",
        None,
        None,
        None,
        None,
        b"partial out",
        b"partial err",
    )

    result = _async.normalize_asyncssh_error(
        error,
        command="sleep",
        timeout=0.5,
    )

    assert isinstance(result, subprocess.TimeoutExpired)
    assert result.cmd == "sleep"
    assert result.timeout == 0.5
    assert result.stdout == b"partial out"
    assert result.stderr == b"partial err"


def test_an_asyncio_connect_timeout_crosses_as_a_builtin_timeout():
    """On 3.9/3.10 -- 3.9 is the declared floor -- `asyncio.TimeoutError` is
    a distinct class, not the builtin, and asyncssh raises exactly that for a
    connect or login timeout (`ConnectTimeout` in ~/.ssh/config, or the
    default 120s `login_timeout`). It matched neither arm of the mapping, so
    it escaped `host.run()` verbatim: the documented
    `except (ConnectionError, TimeoutError)` did not catch it, and
    `SshExecutorProvider.connect()` could not turn it into
    `OperationNotStarted` for the next provider."""
    import asyncio

    result = _async.normalize_asyncssh_error(asyncio.TimeoutError("timed out"))

    assert isinstance(result, TimeoutError)
    assert type(result) is not asyncio.TimeoutError or TimeoutError is (
        asyncio.TimeoutError
    )


def test_an_asyncio_command_timeout_still_becomes_timeout_expired():
    import asyncio

    result = _async.normalize_asyncssh_error(
        asyncio.TimeoutError("timed out"), command="sleep 30", timeout=5
    )

    assert isinstance(result, subprocess.TimeoutExpired)
    assert result.timeout == 5


def test_an_interrupted_bridge_call_cancels_the_coroutine(monkeypatch):
    """`async_to_sync` never cancelled: an exception out of `result()` --
    a Ctrl-C during `run()` is the ordinary one -- returned to the caller
    while the coroutine kept running on the bridge loop, so the remote
    command continued and its channel stayed open."""
    import asyncio

    from hostctl import _async

    cancelled = []

    class _Future:
        def result(self, timeout=None):
            raise KeyboardInterrupt

        def cancel(self):
            cancelled.append(True)
            return True

    async def work():
        await asyncio.sleep(0)

    coroutine = work()
    monkeypatch.setattr(
        _async._asyncio,
        "run_coroutine_threadsafe",
        lambda coro, loop: (coro.close(), _Future())[1],
    )

    with pytest.raises(KeyboardInterrupt):
        _async.async_to_sync(coroutine)

    assert cancelled == [True]


def test_an_undecodable_byte_is_not_reported_as_a_broken_connection():
    """asyncssh decodes with the requested encoding, so a byte the remote
    command emitted that utf-8 cannot decode arrived as `ProtocolError` and
    was mapped to `ConnectionError` -- which reads as "the link broke" and
    invites a reconnect loop that decodes the same byte again forever. The
    answer is `errors=`, so the caller must see that."""
    failure = asyncssh.ProtocolError("'utf-8' codec can't decode byte 0xff")

    result = _async.normalize_asyncssh_error(failure)

    assert not isinstance(result, ConnectionError)
    assert "errors=" in str(result)


def test_a_genuine_protocol_error_is_still_a_connection_error():
    result = _async.normalize_asyncssh_error(asyncssh.ProtocolError("bad packet"))

    assert isinstance(result, ConnectionError)
