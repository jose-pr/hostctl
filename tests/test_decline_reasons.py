"""A provider that declines says why, and the fallback WARNING repeats it.

The decline reason is what `SelectionStep.reason` and the host's one fallback
WARNING show, so an untrusted host key, a refused port, a refused login, a
name that does not resolve and a timeout each read differently -- and none of
them carries a credential.
"""

from __future__ import annotations

import errno
import logging
import socket
import ssl
import subprocess
import threading

import pytest

from hostctl import (
    ExecutorProvider,
    OperationNotStartedError,
    PathProvider,
    PosixConfig,
    PosixHost,
    Timeout,
)
from hostctl.provider._decline import cause, connect_failure

_TRUST = "add the key to known_hosts"


def _execute(command, *args, **options):
    return subprocess.CompletedProcess((command, *args), 0, b"ok", b"")


def _fallback_warning(caplog):
    (record,) = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "fell back" in record.getMessage()
    ]
    return record.getMessage()


# -- the classification ----------------------------------------------------------


def _wrapped(error, outer=ConnectionError):
    """`error` as a transport normalises it: re-raised `from` the original."""
    try:
        try:
            raise error
        except BaseException as inner:
            raise outer(str(inner)) from inner
    except BaseException as caught:
        return caught


class _MaxRetryError(Exception):
    """urllib3's shape: the failure is `.reason`, not the cause chain."""

    def __init__(self, reason):
        super().__init__("Max retries exceeded with url: /wsman")
        self.reason = reason


class _RequestsConnectionError(OSError):
    """requests' shape: the urllib3 error is an exception argument."""


@pytest.mark.parametrize(
    "error, expected",
    [
        pytest.param(
            ConnectionRefusedError(errno.ECONNREFUSED, "Connect call failed"),
            "SSH connection to nas:22 was refused (",
            id="refused",
        ),
        pytest.param(
            socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
            "SSH host name nas did not resolve (",
            id="dns",
        ),
        pytest.param(
            OSError(errno.EHOSTUNREACH, "No route to host"),
            "SSH host nas:22 is unreachable (",
            id="unreachable",
        ),
        pytest.param(
            TimeoutError(), "SSH connection to nas:22 timed out", id="timeout"
        ),
        pytest.param(
            PermissionError("Permission denied for user root on host nas"),
            "SSH login to nas:22 was refused (Permission denied for user root",
            id="login",
        ),
        pytest.param(
            ConnectionResetError(errno.ECONNRESET, "reset by peer"),
            "SSH connection to nas:22 was closed by the peer (",
            id="closed",
        ),
        pytest.param(
            ConnectionError("Connection lost"),
            "SSH connection to nas:22 failed (ConnectionError: Connection lost)",
            id="other",
        ),
    ],
)
def test_each_cause_reads_differently(error, expected):
    reason = connect_failure("SSH", "nas", 22, _wrapped(error), trust=_TRUST)

    assert reason.startswith(expected), reason
    assert _TRUST not in reason


def test_an_untrusted_host_key_names_the_target_and_the_remedy():
    asyncssh = pytest.importorskip("asyncssh")
    error = _wrapped(
        asyncssh.HostKeyNotVerifiable("Host key is not trusted for host nas")
    )

    reason = connect_failure("SSH", "nas", 2222, error, trust=_TRUST)

    assert reason == (
        "SSH host key of nas:2222 is not trusted "
        f"(Host key is not trusted for host nas); {_TRUST}"
    )


def test_a_cause_held_as_a_reason_or_an_argument_is_found():
    """requests -> urllib3 -> socket: neither link is `__cause__`."""
    refused = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
    error = _RequestsConnectionError(_MaxRetryError(refused))

    reason = connect_failure("WinRM", "win", None, error, trust="x")

    assert reason.startswith("WinRM connection to win was refused (")


def test_an_untrusted_certificate_names_the_remedy():
    certificate = ssl.SSLCertVerificationError(1, "certificate verify failed")
    error = _RequestsConnectionError(_MaxRetryError(certificate))

    reason = connect_failure("WinRM", "win", 5986, error, trust="pass verify=False")

    assert reason.startswith("WinRM TLS certificate of win:5986 is not trusted (")
    assert reason.endswith("; pass verify=False")


def test_an_ipv6_target_is_bracketed():
    reason = connect_failure("SSH", "2001:db8::1", 22, TimeoutError(), trust=_TRUST)

    assert reason == "SSH connection to [2001:db8::1]:22 timed out"


def test_credentials_in_an_error_message_are_redacted_and_the_detail_is_one_line():
    error = ConnectionError(
        "failed for ssh://root:hunter2@nas password=hunter2\n" + "x" * 1000
    )

    reason = connect_failure("SSH", "nas", 22, error, trust=_TRUST)

    assert "hunter2" not in reason
    assert "\n" not in reason and len(reason) < 400


# -- the cause of any other decline ------------------------------------------------


def test_a_cause_is_its_type_and_message():
    assert cause(TimeoutError("send timed out")) == "TimeoutError: send timed out"


def test_a_cause_without_a_message_names_the_wrapped_one():
    try:
        try:
            raise ConnectionResetError("peer reset")
        except OSError as inner:
            raise ConnectionError() from inner
    except ConnectionError as outer:
        assert cause(outer) == "ConnectionResetError: peer reset"
    assert cause(TimeoutError()) == "TimeoutError"


def test_a_cause_is_one_redacted_line():
    text = cause(OSError("lost wss://u:hunter2@nas token=hunter2\n" + "x" * 1000))

    assert text.startswith("OSError: lost wss://u:<redacted>@nas")
    assert "hunter2" not in text
    assert "\n" not in text and len(text) < 300


# -- real SSH declines through the in-process server -----------------------------

#: Keeps a local ssh-agent out of it: the server decides which method works.
_NO_AGENT = {"agent_path": None}


@pytest.fixture
def ssh_server():
    pytest.importorskip("asyncssh")
    from conformance.inprocess_ssh import InProcessSSHServer

    async def handler(process):
        process.stdout.write(b"ok\n")
        process.exit(0)

    def make(**options):
        return InProcessSSHServer(handler, **options)

    return make


def _ssh_then_web(config):
    from hostctl.transports.ssh import ssh_providers

    executor, _ = ssh_providers(config)
    return PosixHost(
        PosixConfig("nas1"),
        executor_providers=(executor, ExecutorProvider("web", _execute)),
        path_providers=(),
    )


def _declined(host, caplog):
    with caplog.at_level(logging.WARNING, logger="hostctl"):
        try:
            assert host.run("true").stdout == b"ok"
            step = host.last_selection[0]
        finally:
            host.close()
    assert (step.provider, step.availability) == ("ssh", "unavailable")
    warning = _fallback_warning(caplog)
    assert warning == f"[nas1] run() fell back from ssh to web: ssh: {step.reason}"
    return step.reason


def test_an_untrusted_host_key_is_named_with_its_remedy(ssh_server, tmp_path, caplog):
    empty = tmp_path / "known_hosts"
    empty.write_text("")
    with ssh_server() as server:
        config = server.config(known_hosts=str(empty), connect_options=_NO_AGENT)
        reason = _declined(_ssh_then_web(config), caplog)

    assert reason.startswith(
        f"SSH host key of 127.0.0.1:{server.port} is not trusted ("
    )
    assert "known_hosts=" in reason and "verify=False" in reason


def test_a_refused_login_is_named_and_carries_no_password(ssh_server, caplog):
    with ssh_server(accept_keys=False, password="the-right-one") as server:
        config = server.config(password="hunter2-secret", connect_options=_NO_AGENT)
        reason = _declined(_ssh_then_web(config), caplog)

    assert reason.startswith(f"SSH login to 127.0.0.1:{server.port} was refused (")
    assert "hunter2-secret" not in caplog.text


def test_a_refused_port_is_named(caplog):
    pytest.importorskip("asyncssh")
    from hostctl.transports.ssh import SSHConfig

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = SSHConfig("127.0.0.1", port=port, verify=False, connect_options=_NO_AGENT)

    reason = _declined(_ssh_then_web(config), caplog)

    assert reason.startswith(f"SSH connection to 127.0.0.1:{port} was refused (")


def test_a_server_that_never_answers_times_out(caplog):
    pytest.importorskip("asyncssh")
    from hostctl.transports.ssh import SSHConfig

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    accepted = []
    thread = threading.Thread(
        target=lambda: accepted.append(listener.accept()), daemon=True
    )
    thread.start()
    config = SSHConfig(
        "127.0.0.1",
        port=listener.getsockname()[1],
        verify=False,
        timeout=Timeout(connect=0.5),
        connect_options=_NO_AGENT,
    )
    try:
        reason = _declined(_ssh_then_web(config), caplog)
    finally:
        for connection, _ in accepted:
            connection.close()
        listener.close()

    assert reason == f"SSH connection to 127.0.0.1:{config.port} timed out"


@pytest.mark.parametrize(
    "error, start",
    [
        (
            socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
            "SSH host name nas.invalid did not resolve (",
        ),
        (
            OSError(errno.ENETUNREACH, "Network is unreachable"),
            "SSH host nas.invalid is unreachable (",
        ),
    ],
    ids=["dns", "unreachable"],
)
def test_name_and_route_failures_are_named(monkeypatch, caplog, error, start):
    pytest.importorskip("asyncssh")
    from hostctl.transports.ssh import SSHConfig, ssh_providers

    executor, _ = ssh_providers(SSHConfig("nas.invalid", verify=False))

    def fail():
        raise error

    monkeypatch.setattr(executor.transport, "connect", fail)
    host = PosixHost(
        PosixConfig("nas1"),
        executor_providers=(executor, ExecutorProvider("web", _execute)),
        path_providers=(),
    )

    assert _declined(host, caplog).startswith(start)


def test_a_path_fallback_names_the_ssh_reason(ssh_server, tmp_path, caplog):
    from pathapi import Path

    from hostctl.transports.ssh import ssh_providers

    empty = tmp_path / "known_hosts"
    empty.write_text("")
    (tmp_path / "f").write_bytes(b"data")
    with ssh_server(sftp_root=tmp_path) as server:
        config = server.config(known_hosts=str(empty), connect_options=_NO_AGENT)
        _, sftp = ssh_providers(config)
        host = PosixHost(
            PosixConfig("nas1"),
            executor_providers=(),
            path_providers=(
                sftp,
                PathProvider("local", lambda *parts: Path(tmp_path, *parts)),
            ),
        )
        with caplog.at_level(logging.WARNING, logger="hostctl"):
            try:
                assert host.path("f").read_bytes() == b"data"
            finally:
                host.close()

    warning = _fallback_warning(caplog)
    assert warning.startswith("[nas1] path() fell back from sftp to local: sftp: ")
    assert f"SSH host key of 127.0.0.1:{server.port} is not trusted" in warning


# -- WinRM ------------------------------------------------------------------------


class _Executor:
    executor_capabilities = frozenset()

    def __call__(self, command, *args, **options):
        return _execute(command, *args, **options)


class _WinRMTransport:
    """The surface `WinRMExecutorProvider` reads, failing as requests does."""

    capabilities = frozenset()

    def __init__(self, error):
        from hostctl.transports.winrm import WinRMConfig

        self.config = WinRMConfig("win.example", username="u", password="hunter2")
        self.executor = _Executor()
        self.error = error

    def connect(self):
        raise self.error


def test_a_winrm_certificate_failure_is_named_with_its_remedy(caplog):
    from hostctl.transports.winrm._providers import WinRMExecutorProvider

    certificate = ssl.SSLCertVerificationError(1, "certificate verify failed")
    provider = WinRMExecutorProvider(
        _WinRMTransport(_RequestsConnectionError(_MaxRetryError(certificate)))
    )

    with pytest.raises(OperationNotStartedError) as declined:
        provider.connect()

    assert declined.value.reason.startswith(
        "WinRM TLS certificate of win.example is not trusted ("
    )
    assert "verify=False" in declined.value.reason
    assert "hunter2" not in declined.value.reason
