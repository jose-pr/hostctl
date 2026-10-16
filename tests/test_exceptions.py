"""The exception tree: one base, the builtin mix-ins, and one home."""

from __future__ import annotations

import subprocess

import pytest

import hostctl
from hostctl import exceptions
from hostctl.provider._contracts import ExecutorProvider
from hostctl.system._system import PosixHost
from hostctl.transports.ssh._transport import SSHConfig
from hostctl.exceptions import (
    ConsoleProtocolError,
    HostctlError,
    OperationNotStartedError,
    ProtocolError,
    QGACommandError,
    QGADisconnectedError,
    QGAProtocolError,
    QGATimeoutError,
    TransportError,
)

_EXPORTED = [
    getattr(hostctl, name)
    for name in hostctl.__all__
    if isinstance(getattr(hostctl, name), type)
    and issubclass(getattr(hostctl, name), BaseException)
]


def test_every_exported_exception_is_a_hostctl_error_with_one_home():
    assert sorted(cls.__name__ for cls in _EXPORTED) == sorted(exceptions.__all__)
    for cls in _EXPORTED:
        assert issubclass(cls, HostctlError), cls
        assert cls.__module__ == "hostctl.exceptions", cls
        assert getattr(exceptions, cls.__name__) is cls


@pytest.mark.parametrize(
    ("cls", "builtin"),
    [
        (TransportError, ConnectionError),
        (OperationNotStartedError, ConnectionError),
        (QGADisconnectedError, ConnectionError),
        (ProtocolError, ConnectionError),
        (QGAProtocolError, ConnectionError),
        (ConsoleProtocolError, ConnectionError),
        (QGATimeoutError, TimeoutError),
    ],
)
def test_each_category_is_the_builtin_a_caller_already_catches(cls, builtin):
    assert issubclass(cls, builtin)


def test_tree_shape():
    assert issubclass(OperationNotStartedError, TransportError)
    assert issubclass(QGADisconnectedError, TransportError)
    assert issubclass(QGAProtocolError, ProtocolError)
    assert issubclass(ConsoleProtocolError, ProtocolError)
    assert not issubclass(QGACommandError, (ConnectionError, TimeoutError))


def test_operation_not_started_is_not_a_runtime_error():
    assert not issubclass(OperationNotStartedError, RuntimeError)
    error = OperationNotStartedError("refused", cause=ConnectionRefusedError())
    assert error.reason == "refused"
    assert isinstance(error.cause, ConnectionRefusedError)
    assert OperationNotStartedError().reason == "operation was not started"


def test_qga_command_error_keeps_the_agent_fields():
    error = QGACommandError("GenericError", "no such file", data={"errno": 2})
    assert str(error) == "GenericError: no such file"
    assert (error.error_class, error.description, error.data) == (
        "GenericError",
        "no such file",
        {"errno": 2},
    )


def _refused_ssh_provider():
    from hostctl.transports.ssh._transport import SSHExecutorProvider, _SSHTransport

    transport = _SSHTransport(SSHConfig("refused.invalid", username="root"))

    def refused():
        raise ConnectionRefusedError(10061, "connection refused")

    transport.connect = refused
    return SSHExecutorProvider(transport)


def test_a_refused_ssh_connect_is_caught_as_connection_error():
    host = PosixHost(executor_providers=(_refused_ssh_provider(),))

    with pytest.raises(ConnectionError) as raised:
        host.run("uptime", check=False)

    assert isinstance(raised.value, OperationNotStartedError)
    # The selector's refusal wraps the provider's decline, which keeps the
    # transport error.
    assert isinstance(raised.value.cause, OperationNotStartedError)
    assert isinstance(raised.value.cause.cause, ConnectionRefusedError)


def test_a_refused_ssh_connect_still_falls_through_to_the_next_provider():
    fallback = ExecutorProvider(
        "local",
        lambda command, *args, **options: subprocess.CompletedProcess(
            (command,), 0, b"ok", b""
        ),
    )
    host = PosixHost(executor_providers=(_refused_ssh_provider(), fallback))

    assert host.run("uptime", check=False).stdout == b"ok"


def test_a_provider_connect_raising_not_started_is_not_rewrapped():
    """The connect guard re-raises a decline as it is, cause and all."""
    provider = _refused_ssh_provider()
    original = OperationNotStartedError("already declined")

    def declined():
        raise original

    provider.transport.connect = declined
    with pytest.raises(OperationNotStartedError) as raised:
        provider.connect()

    assert raised.value is original
