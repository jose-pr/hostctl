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
