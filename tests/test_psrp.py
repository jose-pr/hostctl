"""Optional PSRP selection and runspace contract tests."""

from __future__ import annotations

import types

import pytest

from hostctl import (
    ExecutorProvider,
    HostConfig,
    WinRMConfig,
    WindowsHost,
)
from hostctl.executor.psrp import PsrpExecutor, pypsrp_available
from hostctl.process import RunspaceSession
from hostctl.process.psrp import PipelineResult, PipelineStreams


def test_provider_uri_roundtrip_and_secret_safety():
    config = WinRMConfig("host", "user", "secret", provider="psrp")
    restored = HostConfig(str(config), password="secret")
    assert restored.provider == "psrp"
    assert "secret" not in str(config)


def test_explicit_psrp_has_actionable_error_when_missing(monkeypatch):
    monkeypatch.setattr(
        "hostctl.process.psrp.require_pypsrp",
        lambda: (_ for _ in ()).throw(
            ImportError("PSRP requires hostctl[psrp] on Python 3.10+")
        ),
    )
    host = WindowsHost.from_winrm(WinRMConfig("host", "user", provider="psrp"))
    with pytest.raises(ImportError, match="Python 3.10|psrp"):
        host.runspace()


@pytest.mark.parametrize(
    ("state", "had_errors", "normalized_state", "returncode"),
    [
        ("Completed", False, "Completed", 0),
        (4, False, "Completed", 0),
        ("Failed", False, "Failed", 1),
        ("Stopped", False, "Stopped", 1),
    ],
)
def test_runspace_preserves_state_and_typed_streams(
    monkeypatch, state, had_errors, normalized_state, returncode
):
    class Streams:
        error = ["err"]
        warning = ["warn"]
        verbose = ["verbose"]
        debug = ["debug"]
        information = ["info"]
        progress = ["progress"]

    class Pipeline:
        streams = Streams()

        def __init__(self, pool):
            self.pool = pool
            self.state = state
            self.had_errors = had_errors

        def add_script(self, script):
            self.script = script

        def invoke(self):
            self.pool.scripts.append(self.script)
            return [self.pool.scripts[-1]]

    class Pool:
        def __init__(self):
            self.scripts = []

        def open(self):
            self.opened = True

        def close(self):
            self.closed = True

    fake = types.ModuleType("pypsrp")
    fake_powershell = types.ModuleType("pypsrp.powershell")
    fake_powershell.PowerShell = Pipeline
    monkeypatch.setitem(__import__("sys").modules, "pypsrp", fake)
    monkeypatch.setitem(__import__("sys").modules, "pypsrp.powershell", fake_powershell)
    pool = Pool()
    session = RunspaceSession(pool=pool)
    first = session.invoke("$x = 1")
    second = session.invoke("$x")
    assert first.output == ("$x = 1",)
    assert second.output == ("$x",)
    assert first.streams.error == ("err",)
    assert first.streams.warning == ("warn",)
    assert first.streams.verbose == ("verbose",)
    assert first.streams.debug == ("debug",)
    assert first.streams.information == ("info",)
    assert first.streams.progress == ("progress",)
    assert first.state == normalized_state
    assert first.returncode == returncode
    session.close()


def test_runspace_structured_arguments_use_powershell_literals(monkeypatch):
    class Pipeline:
        streams = types.SimpleNamespace(error=[])
        state = "Completed"
        had_errors = False

        def __init__(self, pool):
            self.script = None

        def add_script(self, script):
            self.script = script

        def invoke(self):
            self.output = (self.script,)
            return self.output

    fake = types.ModuleType("pypsrp")
    fake_powershell = types.ModuleType("pypsrp.powershell")
    fake_powershell.PowerShell = Pipeline
    monkeypatch.setitem(__import__("sys").modules, "pypsrp", fake)
    monkeypatch.setitem(__import__("sys").modules, "pypsrp.powershell", fake_powershell)

    session = RunspaceSession(pool=types.SimpleNamespace(open=lambda: None))
    result = session.invoke("C:\\Program Files\\tool.exe", "a’b", "$(danger)")
    assert "'C:\\Program Files\\tool.exe'" in result.output[0]
    assert "'a’’b'" in result.output[0]
    assert "'$(danger)'" in result.output[0]


def test_psrp_executor_projects_objects_and_error_streams():
    class Session:
        def invoke(self, script, *, raw=False, capture_exit=False):
            assert script == "Write-Output x"
            return PipelineResult(
                ("one", "two"),
                PipelineStreams(error=("bad",)),
                "Completed",
                True,
                1,
            )

    result = PsrpExecutor(lambda: Session())("Write-Output x", check=False, text=True)
    assert result.stdout == "one\ntwo\n"
    assert result.stderr == "bad"
    assert result.returncode == 1


def test_psrp_executor_projects_native_last_exit_code():
    """The executor trusts the session's marker handling, and only that.

    It used to re-parse `__HOSTCTL_LASTEXITCODE__` out of output the session
    had already stripped -- one convention with two implementations. The fake
    here now behaves as `invoke(capture_exit=True)` really does: marker
    consumed, code in `returncode`.
    """

    class Session:
        def invoke(self, script, *, raw=False, capture_exit=False):
            assert capture_exit is True
            return PipelineResult(
                ("ok",),
                PipelineStreams(),
                "Completed",
                False,
                7,
            )

    result = PsrpExecutor(lambda: Session())("cmd", check=False, text=True)
    assert result.stdout == "ok\n"
    assert result.returncode == 7


def test_runspace_capture_exit_consumes_the_marker_line(monkeypatch):
    """The single owner of the marker convention, tested directly.

    Both halves matter: the line must not reach the caller as output, and a
    zero exit code reported alongside pipeline errors stays a failure.
    """

    class Pipeline:
        streams = types.SimpleNamespace(error=[])
        state = "Completed"

        def __init__(self, pool):
            self.pool = pool
            self.had_errors = pool.had_errors

        def add_script(self, script):
            self.script = script

        def invoke(self):
            self.pool.scripts.append(self.script)
            # The marker is per call now, so the fake echoes back the one it
            # was actually given -- which is what a remote shell does.
            marker = self.script.rsplit("Write-Output ('", 1)[1].split(":'", 1)[0]
            return ["ok", f"{marker}:{self.pool.code}"]

    class Pool:
        def __init__(self, code, had_errors=False):
            self.scripts = []
            self.code = code
            self.had_errors = had_errors

        def open(self):
            self.opened = True

    fake = types.ModuleType("pypsrp")
    fake_powershell = types.ModuleType("pypsrp.powershell")
    fake_powershell.PowerShell = Pipeline
    monkeypatch.setitem(__import__("sys").modules, "pypsrp", fake)
    monkeypatch.setitem(__import__("sys").modules, "pypsrp.powershell", fake_powershell)

    result = RunspaceSession(pool=Pool(7)).invoke("cmd", capture_exit=True)
    assert result.output == ("ok",)
    assert result.returncode == 7

    # A remote zero beside pipeline errors is still a failure.
    failed = RunspaceSession(pool=Pool(0, had_errors=True)).invoke(
        "cmd", capture_exit=True
    )
    assert failed.returncode == 1

    # The script the pipeline received carries the epilogue, and resets
    # `$LASTEXITCODE` first: this runspace is persistent, so a pure-PowerShell
    # command -- which never sets it -- used to report whatever an earlier
    # native command had left behind, failing a command that succeeded.
    pool = Pool(0)
    RunspaceSession(pool=pool).invoke("cmd", capture_exit=True)
    script = pool.scripts[0]
    assert "__HOSTCTL_LASTEXITCODE_" in script
    assert script.startswith("$global:LASTEXITCODE=0;")
    assert script.index("$global:LASTEXITCODE=0") < script.index(
        "__HOSTCTL_LASTEXITCODE_"
    )
    # On its own LINE: joined with `;`, a payload ending in a `#` comment
    # commented the epilogue out and the pipeline reported a stale status.
    assert script.splitlines()[-1].startswith("Write-Output ('__HOSTCTL_LASTEXITCODE_")


def test_the_runspace_marker_is_per_call():
    """The fixed marker is a string any command may print, and the line after
    it was removed from the output and read as the exit status."""
    from hostctl.executor.winrm import exit_marker

    assert exit_marker() != exit_marker()


def test_runspace_does_not_close_an_injected_pool():
    """An injected pool belongs to its supplier.

    `_owns_pool` was recorded and never read, so a pool shared across
    sessions was closed by whichever session finished first.
    """

    class Pool:
        def __init__(self):
            self.opened = False
            self.closed = False

        def open(self):
            self.opened = True

        def close(self):
            self.closed = True

    injected = Pool()
    session = RunspaceSession(pool=injected)
    session.connect()
    session.close()
    assert injected.opened
    assert not injected.closed
    # The session stays reopenable on the same pool.
    session.connect()
    assert session._pool is injected


def test_runspace_closes_a_pool_it_created(monkeypatch):
    class Pool:
        def __init__(self):
            self.closed = False

        def open(self):
            return None

        def close(self):
            self.closed = True

    created = Pool()
    session = RunspaceSession(config=object())
    monkeypatch.setattr(session, "_make_pool", lambda: created)
    session.connect()
    session.close()
    assert created.closed


def test_psrp_executor_rejects_byte_stream_options():
    executor = PsrpExecutor(lambda: object())
    with pytest.raises(NotImplementedError, match="stdin"):
        executor("x", input=b"x")
    with pytest.raises(NotImplementedError, match="timeout"):
        executor("x", timeout=1)


def test_windows_host_dispatches_psrp_as_a_script_without_nested_powershell():
    scripts = []

    class Session:
        def invoke(self, script, *, raw=False, capture_exit=False):
            scripts.append(script)
            return PipelineResult(
                ("9000",),
                PipelineStreams(),
                "Completed",
                False,
                0,
            )

    executor = PsrpExecutor(lambda: Session())
    host = WindowsHost(
        executor_providers=(ExecutorProvider("psrp", executor),),
    )

    result = host.run("$x='x' * 9000; Write-Output $x.Length")

    assert result.stdout == b"9000\n"
    assert scripts == ["$x='x' * 9000; Write-Output $x.Length"]
    assert "powershell.exe" not in scripts[0].casefold()


@pytest.mark.parametrize("provider", ["psrp", "pywinrm"])
def test_winrm_provider_backed_host_never_double_wraps_powershell(
    monkeypatch, provider
):
    """A provider-backed Windows host dispatches one finalized shell layer.

    Live testing found provider-backed Windows execution nesting
    ``powershell.exe -Command ...`` inside the transport's own PowerShell
    layer.  The outer layer expanded ``$x`` before the inner script ever ran,
    so ``$x='x' * 9000; Write-Output $x.Length`` reported an empty length
    instead of 9000.  This asserts the whole composition -- transport,
    executor capability set, provider, and ``SystemHost`` dispatch -- keeps a
    single layer, which is the seam the bare-``ExecutorProvider`` test above
    does not cover.
    """
    from hostctl.host._winrm import WinRMExecutorProvider, _WinRMTransport

    if provider == "psrp":
        pytest.importorskip(
            "pypsrp", reason="PSRP capability wiring requires the psrp extra"
        )
        if not pypsrp_available():
            pytest.skip("PSRP requires Python 3.10 or newer")

    script = "$x='x' * 9000; Write-Output $x.Length"
    dispatched = []

    class FakeRunspace:
        def invoke(self, value, *, raw=False, capture_exit=False):
            dispatched.append(value)
            return PipelineResult(("9000",), PipelineStreams(), "Completed", False, 0)

    class FakeWinRMSession:
        def run_ps(self, value):
            dispatched.append(value)
            return types.SimpleNamespace(status_code=0, std_out=b"9000\n", std_err=b"")

    config = WinRMConfig("host", username="u", password="p", provider=provider)
    transport = _WinRMTransport(config)
    # Pin the transport to an in-memory session: constructing a real one would
    # open a network connection, which a unit test must never do.
    if provider == "psrp":
        monkeypatch.setattr(transport, "runspace", lambda: FakeRunspace())
    else:
        transport._session = FakeWinRMSession()

    executor_provider = WinRMExecutorProvider(transport)
    # The executor must advertise that it accepts a *finalized script*, which
    # is what suppresses the extra ``powershell.exe -Command`` invocation.
    assert "script" in executor_provider.capabilities
    assert "args" not in executor_provider.capabilities

    host = WindowsHost(config, executor_providers=(executor_provider,))
    result = host.run(script, check=False)

    assert len(dispatched) == 1
    sent = dispatched[0]
    # The regression: the script must arrive intact, not nested inside another
    # PowerShell invocation that would expand $x one layer too early.
    assert "powershell.exe" not in sent.casefold()
    assert "-command" not in sent.casefold()
    assert sent.startswith(script)
    assert result.stdout.strip() == b"9000"


def test_an_injected_pool_is_not_reopened_after_close():
    """`close()` deliberately leaves an injected pool alone, and `connect()`
    then opened it again -- resetting a pool the caller was still using,
    which is the reason to inject one at all."""

    class Pool:
        def __init__(self):
            self.opens = 0

        def open(self):
            self.opens += 1

        def close(self):
            raise AssertionError("an injected pool must not be closed here")

    injected = Pool()
    session = RunspaceSession(pool=injected)
    session.connect()
    session.close()
    session.connect()

    assert injected.opens == 1


def test_two_threads_connecting_build_one_pool(monkeypatch):
    """Both threads built a pool on a fresh session, and the loser's was
    opened and then dropped -- an authenticated connection nothing closes."""
    import threading

    built = []

    class Pool:
        def __init__(self):
            built.append(self)

        def open(self):
            return None

        def close(self):
            return None

    session = RunspaceSession(config=object())
    monkeypatch.setattr(session, "_make_pool", Pool)

    barrier = threading.Barrier(4)

    def connect():
        barrier.wait(5)
        session.connect()

    threads = [threading.Thread(target=connect) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(built) == 1
