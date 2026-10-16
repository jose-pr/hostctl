"""`WinRMConfig(timeout=)` is the default for `run(timeout=)` on every leg.

`connect_timeout` bounds only establishing the connection, and the per-request
timing is internal: none of them may end a command still producing its result.
"""

from __future__ import annotations

import inspect
import math
import socket
import subprocess
import sys
import threading
import time
import types
from urllib.parse import parse_qs, urlsplit

import pytest

from hostctl.host._config import HostConfig
from hostctl.transports.winrm._config import (
    _OPERATION_TIMEOUT,
    _READ_TIMEOUT,
    WinRMConfig,
)
from hostctl.transports.winrm._native import NativeWinRMSession
from hostctl.transports.winrm._psrp import PSRPExecutor, RunspaceSession
from hostctl.transports.winrm._pywinrm import WinRMExecutor, _PywinrmStdinSession
from hostctl.transports.winrm._transport import _WinRMTransport

# --- the config field -------------------------------------------------------


def test_timeout_defaults_to_thirty_seconds_and_is_in_the_signature():
    config = WinRMConfig("h", username="u")
    assert config.timeout == 30.0
    parameter = inspect.signature(WinRMConfig).parameters["timeout"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == 30.0


@pytest.mark.parametrize("value", [0, -1, math.nan])
def test_timeout_must_be_positive(value):
    with pytest.raises(ValueError, match="timeout"):
        WinRMConfig("h", username="u", timeout=value)


def test_timeout_none_is_no_default_deadline():
    assert WinRMConfig("h", username="u", timeout=None).timeout is None


def test_timeout_is_independent_of_connect_timeout():
    config = WinRMConfig("h", username="u", connect_timeout=60, timeout=5)
    assert (config.timeout, config.connect_timeout) == (5, 60)


def test_connect_timeout_defaults_to_thirty_seconds_and_is_in_the_signature():
    config = WinRMConfig("h", username="u")
    assert config.connect_timeout == 30.0
    parameter = inspect.signature(WinRMConfig).parameters["connect_timeout"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == 30.0


@pytest.mark.parametrize("value", [0, -1, math.nan])
def test_connect_timeout_must_be_positive(value):
    with pytest.raises(ValueError, match="connect_timeout"):
        WinRMConfig("h", username="u", connect_timeout=value)


def test_connect_timeout_none_is_the_library_default():
    assert WinRMConfig("h", username="u", connect_timeout=None).connect_timeout is None


@pytest.mark.parametrize(
    ("value", "spelled"),
    [
        (45.0, "connect_timeout=45"),
        (2.5, "connect_timeout=2.5"),
        (None, "connect_timeout="),
    ],
)
def test_a_non_default_connect_timeout_round_trips_through_the_uri(value, spelled):
    config = WinRMConfig("h", username="u", connect_timeout=value)
    assert spelled in config.connection_uri
    restored = HostConfig(config.connection_uri)
    assert restored.connect_timeout == value
    assert restored == config


def test_the_default_connect_timeout_stays_out_of_the_uri():
    query = parse_qs(urlsplit(WinRMConfig("h", username="u").connection_uri).query)
    assert "connect_timeout" not in query


def test_a_bad_connect_timeout_in_the_uri_raises():
    with pytest.raises(ValueError, match="connect_timeout"):
        HostConfig("winrm://u@h?connect_timeout=soon")
    with pytest.raises(ValueError, match="connect_timeout"):
        HostConfig("winrm://u@h?connect_timeout=0")


@pytest.mark.parametrize("name", ["read_timeout", "operation_timeout"])
def test_the_per_request_timeouts_are_not_keywords(name):
    with pytest.raises(TypeError, match=name):
        WinRMConfig("h", username="u", **{name: 30})


@pytest.mark.parametrize(
    "name",
    ["read_timeout", "operation_timeout", "read_timeout_sec", "operation_timeout_sec"],
)
def test_a_per_request_timeout_in_the_uri_names_the_fields_to_use(name):
    with pytest.raises(ValueError, match="unknown connection parameter") as raised:
        HostConfig(f"winrm://u@h?{name}=30")
    assert name in str(raised.value)
    assert "connect_timeout" in str(raised.value)
    assert "timeout (the whole command)" in str(raised.value)


def test_the_read_limit_outlasts_the_server_hold():
    assert _READ_TIMEOUT > _OPERATION_TIMEOUT


@pytest.mark.parametrize(
    ("value", "spelled"),
    [(45.0, "timeout=45"), (2.5, "timeout=2.5"), (None, "timeout=")],
)
def test_a_non_default_timeout_round_trips_through_the_uri(value, spelled):
    config = WinRMConfig("h", username="u", timeout=value)
    assert spelled in config.connection_uri
    restored = HostConfig(config.connection_uri)
    assert restored.timeout == value
    assert restored == config


def test_the_default_timeout_stays_out_of_the_uri():
    query = parse_qs(urlsplit(WinRMConfig("h", username="u").connection_uri).query)
    assert "timeout" not in query


def test_a_bad_timeout_in_the_uri_raises():
    with pytest.raises(ValueError, match="timeout"):
        HostConfig("winrm://u@h?timeout=soon")
    with pytest.raises(ValueError, match="timeout"):
        HostConfig("winrm://u@h?timeout=0")


# --- native leg -------------------------------------------------------------


def _native_transport(monkeypatch, **options):
    import hostctl.transports.winrm._transport as winrm_module

    monkeypatch.setattr(winrm_module.os, "name", "nt")
    monkeypatch.setenv("USERNAME", "admin")
    monkeypatch.delenv("USERDOMAIN", raising=False)
    return _WinRMTransport(WinRMConfig("server", username="admin", **options))


class _Completed:
    returncode = 0
    stdout = b"ok"
    stderr = b""


def _record_native_deadlines(monkeypatch):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(kwargs["timeout"])
        return _Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_native_default_deadline_is_timeout_not_connect_timeout(monkeypatch):
    transport = _native_transport(monkeypatch, connect_timeout=31, timeout=600)
    seen = _record_native_deadlines(monkeypatch)
    transport.run("Write-Output x")
    assert seen == [600]


def test_native_run_timeout_overrides_the_default(monkeypatch):
    transport = _native_transport(monkeypatch, timeout=600)
    seen = _record_native_deadlines(monkeypatch)
    transport.run("Write-Output x", timeout=2)
    transport.run("Write-Output x")
    assert seen == [2, 600]


def test_native_timeout_none_sets_no_deadline(monkeypatch):
    transport = _native_transport(monkeypatch, timeout=None)
    seen = _record_native_deadlines(monkeypatch)
    transport.run("Write-Output x")
    assert seen == [None]


def test_native_connect_timeout_bounds_opening_the_session(monkeypatch):
    transport = _native_transport(monkeypatch, connect_timeout=45.5)
    wrapper = transport._leg.session._wrapper("Write-Output x")
    assert "New-PSSessionOption -OpenTimeout 45500;" in wrapper
    assert "SessionOption=$so" in wrapper


def test_native_connect_timeout_none_leaves_powershells_default(monkeypatch):
    transport = _native_transport(monkeypatch, connect_timeout=None)
    wrapper = transport._leg.session._wrapper("Write-Output x")
    assert "-OpenTimeout" not in wrapper


def test_native_open_timeout_is_whole_milliseconds_within_int32():
    tiny = NativeWinRMSession("h", open_timeout=0.0001)._wrapper("x")
    assert "-OpenTimeout 1;" in tiny
    huge = NativeWinRMSession("h", open_timeout=1e12)._wrapper("x")
    assert f"-OpenTimeout {2**31 - 1};" in huge


def test_native_open_timeout_joins_the_certificate_options():
    wrapper = NativeWinRMSession("h", ssl=True, verify=False, open_timeout=3)._wrapper(
        "x"
    )
    assert "New-PSSessionOption -SkipCACheck -SkipCNCheck -OpenTimeout 3000;" in (
        wrapper
    )


def test_native_expiry_reports_the_deadline_it_enforced(monkeypatch):
    transport = _native_transport(monkeypatch, connect_timeout=31, timeout=7)

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        transport.run("Write-Output x")
    assert raised.value.timeout == 7
    assert raised.value.orphaned is True


# --- pywinrm leg ------------------------------------------------------------


class _Protocol:
    """pywinrm's `Protocol` shape: each Receive returns, faults or is done."""

    def __init__(self, receives, *, receive_delay=0.0, cleanup_fails=False):
        self.receives = list(receives)
        self.receive_delay = receive_delay
        self.cleanup_fails = cleanup_fails
        self.calls = []

    def open_shell(self):
        self.calls.append("open_shell")
        return "S"

    def run_command(self, shell, command, arguments=(), **flags):
        return "C"

    def send_command_input(self, shell, command_id, data, end=False):
        pass

    def get_command_output_raw(self, shell, command_id):
        from winrm.exceptions import WinRMOperationTimeoutError

        self.calls.append("receive")
        time.sleep(self.receive_delay)
        item = self.receives.pop(0) if self.receives else "silent"
        if item == "silent":
            raise WinRMOperationTimeoutError()
        return item

    def cleanup_command(self, shell, command_id):
        self.calls.append("cleanup_command")
        if self.cleanup_fails:
            raise ConnectionError("gone")

    def close_shell(self, shell):
        self.calls.append("close_shell")


@pytest.fixture
def _pywinrm():
    pytest.importorskip("winrm", reason="the pywinrm leg needs the winrm extra")


def test_pywinrm_collects_output_across_silent_receives(_pywinrm):
    protocol = _Protocol(
        ["silent", (b"a", b"", 0, False), "silent", (b"b", b"e", 4, True)]
    )
    response = _PywinrmStdinSession(protocol, timeout=None).run_ps("x")
    assert (response.std_out, response.std_err, response.status_code) == (
        b"ab",
        b"e",
        4,
    )


def test_pywinrm_deadline_terminates_the_command_and_closes_the_shell(_pywinrm):
    protocol = _Protocol([], receive_delay=0.05)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        _PywinrmStdinSession(protocol, timeout=0.3).run_ps("Start-Sleep 60")
    assert time.monotonic() - started < 2
    assert raised.value.timeout == 0.3
    assert raised.value.orphaned is True
    assert protocol.calls[-2:] == ["cleanup_command", "close_shell"]


def test_pywinrm_per_call_timeout_overrides_the_session_default(_pywinrm):
    protocol = _Protocol([], receive_delay=0.05)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        _PywinrmStdinSession(protocol, timeout=600).run_ps("x", timeout=0.2)
    assert raised.value.timeout == 0.2


def test_pywinrm_a_failed_cleanup_does_not_hide_the_timeout(_pywinrm):
    protocol = _Protocol([], receive_delay=0.05, cleanup_fails=True)
    with pytest.raises(subprocess.TimeoutExpired):
        _PywinrmStdinSession(protocol, timeout=0.2).run_ps("x")
    assert protocol.calls[-1] == "close_shell"


def _fake_winrm_session(monkeypatch):
    built = []

    class _Session:
        def __init__(self, endpoint, **kwargs):
            built.append(kwargs)
            self.protocol = types.SimpleNamespace(
                transport=types.SimpleNamespace(
                    read_timeout_sec=kwargs["read_timeout_sec"]
                )
            )

    monkeypatch.setitem(sys.modules, "winrm", types.SimpleNamespace(Session=_Session))
    return built


def _pywinrm_transport(**options):
    return _WinRMTransport(
        WinRMConfig("h", username="u", password="p", provider="pywinrm", **options)
    )


def test_pywinrm_leg_builds_its_session_with_the_configured_timeout(monkeypatch):
    _fake_winrm_session(monkeypatch)
    assert _pywinrm_transport(timeout=90)._leg.session.timeout == 90


def test_pywinrm_leg_uses_the_internal_request_timing(monkeypatch):
    built = _fake_winrm_session(monkeypatch)
    _ = _pywinrm_transport()._leg.session
    assert built[0]["operation_timeout_sec"] == _OPERATION_TIMEOUT == 20
    assert built[0]["read_timeout_sec"] == _READ_TIMEOUT == 30


def test_pywinrm_leg_splits_connect_from_read(monkeypatch):
    _fake_winrm_session(monkeypatch)
    session = _pywinrm_transport(connect_timeout=4.5)._leg.session
    assert session._protocol.transport.read_timeout_sec == (4.5, _READ_TIMEOUT)


def test_pywinrm_connect_timeout_none_keeps_pywinrms_single_timeout(monkeypatch):
    _fake_winrm_session(monkeypatch)
    session = _pywinrm_transport(connect_timeout=None)._leg.session
    assert session._protocol.transport.read_timeout_sec == _READ_TIMEOUT


class _HTTPServer:
    """Accepts, reads one request, then answers 500 after `delay` or never."""

    def __init__(self, delay):
        self.delay = delay
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        self._held = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            self._held.append(conn)
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn):
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                data += conn.recv(65536)
            head, _, body = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            while len(body) < length:
                body += conn.recv(65536)
            if self.delay is None:
                return
            time.sleep(self.delay)
            conn.sendall(
                b"HTTP/1.1 500 Internal Server Error\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
        except OSError:
            pass

    def close(self):
        self._listener.close()
        for conn in self._held:
            conn.close()


@pytest.fixture
def _short_request_timing(monkeypatch, _pywinrm):
    """A 1 s server hold and a 2 s read limit, the pair pywinrm accepts."""
    import hostctl.transports.winrm._transport as winrm_module

    monkeypatch.setattr(winrm_module, "_OPERATION_TIMEOUT", 1)
    monkeypatch.setattr(winrm_module, "_READ_TIMEOUT", 2)
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


def _real_pywinrm_transport(port, connect_timeout):
    return _WinRMTransport(
        WinRMConfig(
            "127.0.0.1",
            username="u",
            password="p",
            port=port,
            auth="basic",
            cleartext_auth=True,
            provider="pywinrm",
            connect_timeout=connect_timeout,
        )
    )


def test_pywinrm_a_slow_reply_is_governed_by_the_read_limit(_short_request_timing):
    import winrm.exceptions

    server = _HTTPServer(delay=0.8)
    try:
        protocol = _real_pywinrm_transport(server.port, 0.2)._leg.session._protocol
        started = time.monotonic()
        # The 500 arrives after 0.8 s: past connect_timeout, within the read limit.
        with pytest.raises(winrm.exceptions.WinRMTransportError):
            protocol.open_shell()
        assert time.monotonic() - started >= 0.7
    finally:
        server.close()


def test_pywinrm_a_silent_server_ends_at_the_read_limit(_short_request_timing):
    import requests

    server = _HTTPServer(delay=None)
    try:
        protocol = _real_pywinrm_transport(server.port, 0.2)._leg.session._protocol
        started = time.monotonic()
        with pytest.raises(requests.exceptions.ReadTimeout):
            protocol.open_shell()
        assert 1.8 <= time.monotonic() - started < 10
    finally:
        server.close()


def test_pywinrm_a_never_accepting_host_fails_within_connect_timeout(
    _short_request_timing, monkeypatch
):
    import urllib3.util.connection

    seen = []

    def never_accepts(address, timeout=None, *args, **kwargs):
        seen.append(timeout)
        time.sleep(timeout)
        raise socket.timeout("timed out")

    monkeypatch.setattr(urllib3.util.connection, "create_connection", never_accepts)
    transport = _real_pywinrm_transport(5985, 0.3)
    started = time.monotonic()
    with pytest.raises(ConnectionError):
        transport.run("Write-Output x")
    assert time.monotonic() - started < 1.5
    assert seen and set(seen) == {0.3}


def test_pywinrm_leg_accepts_run_timeout(monkeypatch):
    seen = []

    class _Session:
        def run_ps(self, script, timeout=None):
            seen.append(timeout)
            return types.SimpleNamespace(status_code=0, std_out=b"", std_err=b"")

    transport = _WinRMTransport(
        WinRMConfig("h", username="u", password="p", provider="pywinrm")
    )
    transport._leg._session = _Session()
    transport.run("Write-Output x", timeout=3)
    transport.run("Write-Output x")
    # Without `timeout=` the session applies its own default.
    assert seen == [3, None]


def test_pywinrm_request_timeout_is_reported_against_the_read_limit(monkeypatch):
    class _Session:
        def run_ps(self, script, timeout=None):
            raise TimeoutError("read timed out")

    transport = _WinRMTransport(
        WinRMConfig("h", username="u", password="p", provider="pywinrm")
    )
    transport._leg._session = _Session()
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        transport.run("Write-Output x")
    assert raised.value.timeout == 30.0


def test_a_session_without_a_deadline_still_refuses_timeout():
    class _Plain:
        def run_ps(self, script):
            raise AssertionError("not reached")

    with pytest.raises(NotImplementedError, match="timeout"):
        WinRMExecutor(lambda: _Plain())("hostname", timeout=3)


# --- PSRP leg ---------------------------------------------------------------


class _Pipeline:
    """pypsrp's `PowerShell`: begin, poll with a WS-Man timeout, stop."""

    instances: list = []

    def __init__(self, pool):
        self.pool = pool
        self.state = "NotStarted"
        self.had_errors = False
        self.output = []
        self.polls = []
        self.stopped = False
        self.streams = types.SimpleNamespace()
        _Pipeline.instances.append(self)

    def add_script(self, script):
        self.script = script

    def invoke(self):
        self.state = "Completed"
        return ["untimed"]

    def begin_invoke(self):
        self.state = "Running"

    def poll_invoke(self, timeout=None):
        self.polls.append(timeout)
        time.sleep(0.05)
        if len(self.polls) >= self.pool.finish_after:
            self.output.append("timed")
            self.state = "Completed"

    def stop(self):
        self.stopped = True
        self.state = "Stopped"
        if self.pool.stop_fails:
            raise ConnectionError("gone")


class _Pool:
    def __init__(self, finish_after=10**9, stop_fails=False):
        self.finish_after = finish_after
        self.stop_fails = stop_fails

    def open(self):
        pass


@pytest.fixture
def _fake_pypsrp(monkeypatch):
    _Pipeline.instances = []
    module = types.ModuleType("pypsrp.powershell")
    module.PowerShell = _Pipeline
    monkeypatch.setitem(sys.modules, "pypsrp", types.ModuleType("pypsrp"))
    monkeypatch.setitem(sys.modules, "pypsrp.powershell", module)


def _runspace(pool, **options):
    config = WinRMConfig("win.example", username="u", password="p", **options)
    return RunspaceSession(config, pool=pool)


def test_psrp_invoke_without_timeout_is_untimed(_fake_pypsrp):
    result = _runspace(_Pool()).invoke("x")
    assert result.output == ("untimed",)


def test_psrp_invoke_within_the_deadline_returns_the_output(_fake_pypsrp):
    result = _runspace(_Pool(finish_after=2)).invoke("x", timeout=100)
    assert result.output == ("timed",)
    # Each Receive waits at most the operation timeout.
    assert _Pipeline.instances[-1].polls == [20, 20]


def test_psrp_polls_never_outlast_the_deadline(_fake_pypsrp):
    _runspace(_Pool(finish_after=1)).invoke("x", timeout=2.5)
    assert _Pipeline.instances[-1].polls == [3]


@pytest.mark.parametrize(("value", "expected"), [(7.5, 7.5), (None, 30)])
def test_psrp_pool_takes_connect_timeout_and_the_internal_timing(value, expected):
    pytest.importorskip("pypsrp.powershell")
    config = WinRMConfig("h", username="u", password="p", connect_timeout=value)
    wsman = RunspaceSession(config)._make_pool().connection
    # `None` is pypsrp's own default, which is 30 s.
    assert wsman.transport.connection_timeout == expected
    assert wsman.transport.read_timeout == _READ_TIMEOUT
    assert wsman.operation_timeout == _OPERATION_TIMEOUT


def test_psrp_deadline_stops_the_pipeline(_fake_pypsrp):
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        _runspace(_Pool()).invoke("x", timeout=0.3)
    assert time.monotonic() - started < 2
    assert _Pipeline.instances[-1].stopped
    assert raised.value.timeout == 0.3
    assert raised.value.orphaned is True
    assert all(poll == 1 for poll in _Pipeline.instances[-1].polls)


def test_psrp_a_failed_stop_does_not_hide_the_timeout(_fake_pypsrp):
    with pytest.raises(subprocess.TimeoutExpired):
        _runspace(_Pool(stop_fails=True)).invoke("x", timeout=0.2)


def test_psrp_executor_applies_the_default_and_the_per_call_timeout():
    seen = []

    class _Session:
        def invoke(self, script, *, timeout=None, **options):
            seen.append(timeout)
            from hostctl.transports.winrm._psrp import PipelineResult, PipelineStreams

            return PipelineResult((), PipelineStreams(), "Completed", False, 0)

    executor = PSRPExecutor(lambda: _Session(), lambda: 30.0)
    executor("x")
    executor("x", timeout=4)
    assert seen == [30.0, 4]


def test_psrp_executor_names_the_command_in_its_timeout():
    class _Session:
        def invoke(self, script, **options):
            raise subprocess.TimeoutExpired("win.example", 4)

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        PSRPExecutor(lambda: _Session())("Get-Date", timeout=4)
    assert raised.value.cmd == "Get-Date"
    assert raised.value.timeout == 4
    assert raised.value.orphaned is True


def test_psrp_leg_passes_the_configured_timeout(monkeypatch):
    seen = []

    class _Runspace:
        def invoke(self, script, *, timeout=None, **options):
            seen.append(timeout)
            from hostctl.transports.winrm._psrp import PipelineResult, PipelineStreams

            return PipelineResult((), PipelineStreams(), "Completed", False, 0)

    transport = _WinRMTransport(
        WinRMConfig("h", username="u", password="p", provider="psrp", timeout=None)
    )
    monkeypatch.setattr(transport._leg, "runspace", lambda: _Runspace())
    transport.run("Write-Output x")
    transport.run("Write-Output x", timeout=6)
    assert seen == [None, 6]
