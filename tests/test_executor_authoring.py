"""`BufferedExecutor` and `StreamingExecutor`: the executor authoring bases.

A transport written on them implements only its primitive and must then
behave like every built-in executor, because a `SystemHost` can dispatch the
same call through any of its providers. The fakes here behave like a real
remote: output arrives in pieces, a silent command really is silent until its
deadline, and a connection can drop after the command went out.
"""

from __future__ import annotations

import io
import queue
import re
import subprocess
import time
from pathlib import Path

import pytest

from hostctl import (
    ExecutorProvider,
    OperationNotStartedError,
    PosixHost,
    PosixConfig,
    Timeout,
)
from hostctl.executor import (
    BufferedExecutor,
    ExecutorCapability,
    ExecutorRequest,
    StreamingExecutor,
)


class Recorder(BufferedExecutor):
    """Answers every command with a fixed reply and keeps the request."""

    def __init__(self, reply=(b"out\n", b"err\n", 0)):
        self.reply = reply
        self.requests = []

    def _exchange(self, request):
        self.requests.append(request)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


class Native(Recorder):
    executor_capabilities = frozenset(
        (ExecutorCapability.ARGS, ExecutorCapability.CWD, ExecutorCapability.ENV)
    )


# -- the buffered base ------------------------------------------------------


def test_output_is_bytes_unless_the_call_asks_for_text():
    executor = Recorder()

    assert executor("cmd").stdout == b"out\n"
    assert executor("cmd", text=True).stdout == "out\n"
    # `errors=` alone selects text, as `wants_text` rules for every transport.
    assert executor("cmd", errors="replace").stderr == "err\n"


def test_a_str_reply_is_encoded_under_a_binary_call():
    executor = Recorder(("päth\r\n", "", 0))

    assert executor("cmd").stdout == "päth\r\n".encode()
    assert executor("cmd", encoding="latin-1").stdout == "päth\r\n"


def test_an_empty_captured_stream_is_empty_not_none():
    result = Recorder((None, None, 0))("cmd")

    assert (result.stdout, result.stderr) == (b"", b"")


def test_uncaptured_output_goes_to_its_target():
    out, err = io.BytesIO(), io.StringIO()

    result = Recorder()("cmd", stdout=out, stderr=err, capture_output=False)

    assert (result.stdout, result.stderr) == (None, None)
    assert out.getvalue() == b"out\n"
    assert err.getvalue() == "err\n"


def test_none_targets_mean_the_console(capfd):
    Recorder()("cmd", capture_output=False)

    assert capfd.readouterr() == ("out\n", "err\n")


def test_stderr_to_stdout_merges_and_says_so():
    executor = Recorder()

    result = executor("cmd", stderr=subprocess.STDOUT)

    assert executor.requests[-1].merge_stderr is True
    assert (result.stdout, result.stderr) == (b"out\nerr\n", None)


def test_capture_output_stdout_only():
    result = Recorder()("cmd", capture_output="stdout", stderr=subprocess.DEVNULL)

    assert (result.stdout, result.stderr) == (b"out\n", None)


@pytest.mark.parametrize(
    "value", ["data", b"data", bytearray(b"data"), memoryview(b"data")]
)
def test_input_reaches_the_transport_as_bytes(value):
    executor = Recorder()

    executor("cmd", input=value, text=True)

    assert executor.requests[-1].input == b"data"


def test_stdin_is_read_to_eof_and_devnull_is_empty_input():
    executor = Recorder()

    executor("cmd", stdin=io.BytesIO(b"from a stream"))
    executor("cmd", stdin=subprocess.DEVNULL)
    executor("cmd")

    assert [request.input for request in executor.requests] == [
        b"from a stream",
        b"",
        None,
    ]


def test_input_and_stdin_together_are_refused():
    with pytest.raises(ValueError, match="stdin and input"):
        Recorder()("cmd", input=b"x", stdin=io.BytesIO(b"y"))


def test_a_transport_without_input_refuses_before_reading_the_stream():
    class NoInput(Recorder):
        input_unsupported = "this transport has no stdin"

    stream = io.BytesIO(b"keep me")
    with pytest.raises(NotImplementedError, match="this transport has no stdin"):
        NoInput()("cmd", stdin=stream)

    assert stream.read() == b"keep me"


def test_undeclared_native_features_are_refused():
    executor = Recorder()

    with pytest.raises(NotImplementedError, match="native arguments"):
        executor("cmd", "arg")
    with pytest.raises(NotImplementedError, match="working directory"):
        executor("cmd", cwd="/tmp")
    with pytest.raises(NotImplementedError, match="environment"):
        executor("cmd", env={"A": "1"})
    assert executor.requests == []


def test_declared_native_features_reach_the_request():
    executor = Native()

    result = executor(Path("prog"), "a b", Path("c"), cwd=Path("/work"), env={"A": 1})

    request = executor.requests[-1]
    assert (request.command, request.args) == ("prog", ("a b", "c"))
    assert (request.cwd, request.env) == (str(Path("/work")), {"A": "1"})
    # An argv executor reports argv, as subprocess does.
    assert result.args == ["prog", "a b", "c"]


def test_an_invalid_environment_name_is_refused_before_dispatch():
    executor = Native()

    with pytest.raises(ValueError, match="invalid environment variable name"):
        executor("cmd", env={"A-B": "1"})
    assert executor.requests == []


def test_an_unknown_option_is_a_type_error():
    with pytest.raises(TypeError, match="unsupported Recorder option: shell"):
        Recorder()("cmd", shell=True)


def test_check_defaults_to_true_and_a_missing_status_is_minus_one():
    executor = Recorder((b"", b"boom", None))

    with pytest.raises(subprocess.CalledProcessError) as caught:
        executor("cmd")
    assert caught.value.returncode == -1
    assert caught.value.stderr == b"boom"
    assert executor("cmd", check=False).returncode == -1


def test_an_omitted_timeout_is_the_executor_default_and_none_is_no_limit():
    class Defaulted(Recorder):
        def _default_timeout(self):
            return 30.0

    executor = Defaulted()
    executor("cmd")
    executor("cmd", timeout=None)
    executor("cmd", timeout=2)

    assert [request.timeout for request in executor.requests] == [30.0, None, 2]
    first, second, third = executor.requests
    assert second.deadline is None and second.remaining() is None
    assert 0 < third.remaining() <= 2


def test_a_transport_without_timeouts_refuses_only_a_given_limit():
    class Unbounded(Recorder):
        timeout_unsupported = "no cancellable timeout here"

    executor = Unbounded()
    executor("cmd")
    executor("cmd", timeout=None)
    with pytest.raises(NotImplementedError, match="no cancellable timeout here"):
        executor("cmd", timeout=5)


def test_a_transport_timeout_is_raised_in_hostctl_s_shape():
    raw = subprocess.TimeoutExpired("whatever", 4, output=b"part", stderr=None)
    executor = Recorder(raw)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        executor("cmd", timeout=4, text=True)

    error = caught.value
    assert (error.cmd, error.timeout) == ("cmd", 4)
    assert (error.output, error.stderr) == ("part", "")
    # Nothing said the transport stopped it, so it may still be running.
    assert (error.orphaned, error.pid) == (True, None)
    assert error.__cause__ is raw


def test_a_torn_character_in_partial_output_does_not_hide_the_timeout():
    executor = Recorder(subprocess.TimeoutExpired("cmd", 1, output="é".encode()[:1]))

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        executor("cmd", timeout=1, text=True)

    assert caught.value.output == "�"


def test_normalize_error_maps_transport_failures():
    class Mapped(Recorder):
        def _normalize_error(self, exc):
            if isinstance(exc, KeyError):
                return ConnectionError("lost")
            if isinstance(exc, LookupError):
                return subprocess.TimeoutExpired("x", 9)
            return exc

    with pytest.raises(ConnectionError, match="lost") as caught:
        Mapped(KeyError("k"))("cmd")
    assert isinstance(caught.value.__cause__, KeyError)

    with pytest.raises(subprocess.TimeoutExpired) as timed_out:
        Mapped(IndexError("i"))("cmd")
    assert (timed_out.value.cmd, timed_out.value.orphaned) == ("cmd", True)

    with pytest.raises(RuntimeError, match="as is"):
        Mapped(RuntimeError("as is"))("cmd")


def test_a_decline_passes_through_untouched():
    decline = OperationNotStartedError("not connected")

    with pytest.raises(OperationNotStartedError) as caught:
        Recorder(decline)("cmd")

    assert caught.value is decline


def test_the_provider_reads_the_declared_capabilities():
    provider = ExecutorProvider("native", Native())

    assert provider.capabilities == {"args", "cwd", "env"}


def test_a_host_sends_one_rendered_command_to_a_non_argv_executor():
    executor = Recorder((b"hello\n", b"", 0))
    host = PosixHost(executor_providers=(ExecutorProvider("rec", executor),))

    result = host.run("echo hello", text=True)

    assert result.stdout == "hello\n"
    assert executor.requests[-1].command == "/bin/sh -c 'echo hello'"


def test_a_host_leaves_an_omitted_timeout_to_the_executor_unless_configured():
    class Defaulted(Recorder):
        def _default_timeout(self):
            return 30.0

    executor = Defaulted((b"", b"", 0))
    provider = ExecutorProvider("rec", executor)
    PosixHost(executor_providers=(provider,)).run("true")
    configured = PosixConfig(timeout=Timeout(7))
    PosixHost(configured, executor_providers=(provider,)).run("true")
    PosixHost(configured, executor_providers=(provider,)).run("true", timeout=None)

    assert [request.timeout for request in executor.requests] == [30.0, 7, None]


# -- the streaming base -----------------------------------------------------


class Remote:
    """A command channel: the remote's frames arrive over time, in pieces."""

    def __init__(self, frames=(), *, refuse_send=False, drop_after=None):
        self.frames = queue.Queue()
        for frame in frames:
            self.frames.put(frame)
        self.refuse_send = refuse_send
        self.drop_after = drop_after
        self.sent = []
        self.events = []
        self.interrupted = False
        self.closed = False

    def send(self, data):
        if self.refuse_send:
            raise OSError("connection refused")
        self.sent.append(data)

    def recv(self, timeout):
        if self.drop_after is not None:
            if self.drop_after == 0:
                raise ConnectionResetError("connection reset by peer")
            self.drop_after -= 1
        try:
            frame = self.frames.get(timeout=timeout)
        except queue.Empty:
            return None
        self.events.append(("recv", frame))
        return frame


class Channel(StreamingExecutor):
    def __init__(self, remote, *, kills=True):
        self.remote = remote
        self.kills = kills
        self.abandoned = 0

    def _send(self, request):
        try:
            self.remote.send((request.command, request.input))
        except OSError as exc:
            raise OperationNotStartedError(f"send failed: {exc}", cause=exc) from exc

    def _receive(self, request, wait):
        frame = self.remote.recv(wait)
        if frame is None:
            return b"", b"", None
        kind, value = frame
        if kind == "out":
            return value, b"", None
        if kind == "err":
            return b"", value, None
        return b"", b"", value

    def _abandon(self, request):
        self.abandoned += 1
        self.remote.interrupted = True
        return not self.kills


class Recording(io.BytesIO):
    def __init__(self, remote):
        super().__init__()
        self.remote = remote

    def write(self, data):
        self.remote.events.append(("write", bytes(data)))
        return super().write(data)


def test_captured_output_is_collected_until_the_status():
    remote = Remote([("out", b"a"), ("err", b"e"), ("out", b"b"), ("exit", 3)])

    result = Channel(remote)("cmd", input="in", check=False)

    assert remote.sent == [("cmd", b"in")]
    assert (result.stdout, result.stderr, result.returncode) == (b"ab", b"e", 3)


def test_streamed_output_is_written_as_it_arrives():
    remote = Remote([("out", b"first"), ("out", b"second"), ("exit", 0)])
    sink = Recording(remote)

    result = Channel(remote)("cmd", stdout=sink, capture_output=False)

    assert result.stdout is None
    assert remote.events == [
        ("recv", ("out", b"first")),
        ("write", b"first"),
        ("recv", ("out", b"second")),
        ("write", b"second"),
        ("recv", ("exit", 0)),
    ]


def test_streamed_text_is_decoded_across_split_characters():
    data = "naïve ☃\n".encode()
    remote = Remote([("out", data[:3]), ("out", data[3:9]), ("out", data[9:])])
    remote.frames.put(("exit", 0))
    sink = io.StringIO()

    Channel(remote)("cmd", stdout=sink, encoding="utf-8")

    assert sink.getvalue() == "naïve ☃\n"


def test_merged_stderr_interleaves_by_arrival():
    remote = Remote([("out", b"1"), ("err", b"2"), ("out", b"3"), ("exit", 0)])

    result = Channel(remote)("cmd", stderr=subprocess.STDOUT, text=True)

    assert (result.stdout, result.stderr) == ("123", None)


def test_a_silent_command_times_out_with_its_partial_output():
    remote = Remote([("out", b"started\n"), ("err", "é".encode()[:1])])
    executor = Channel(remote)

    began = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        executor("cmd", timeout=0.3, text=True)

    assert 0.3 <= time.monotonic() - began < 5
    error = caught.value
    assert (error.cmd, error.timeout) == ("cmd", 0.3)
    assert (error.output, error.stderr) == ("started\n", "�")
    # `_abandon` stopped it, so nothing is left running.
    assert (executor.abandoned, remote.interrupted, error.orphaned) == (1, True, False)
    assert error.__cause__ is None


def test_a_timeout_the_transport_cannot_stop_is_orphaned():
    executor = Channel(Remote(), kills=False)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        executor("cmd", timeout=0.05)

    assert caught.value.orphaned is True
    assert (caught.value.output, caught.value.stderr) == (b"", b"")


def test_a_decline_while_sending_moves_to_the_next_provider():
    refused = Channel(Remote(refuse_send=True))
    fallback = Channel(Remote([("out", b"ok"), ("exit", 0)]))
    host = PosixHost(
        executor_providers=(
            ExecutorProvider("first", refused),
            ExecutorProvider("second", fallback),
        )
    )

    result = host.run("true")

    assert result.stdout == b"ok"
    assert refused.abandoned == 0
    assert [item.provider for item in host.last_selection if item.chosen] == ["second"]


def test_a_decline_after_the_command_was_sent_is_not_replayed():
    class LateDecline(Channel):
        def _receive(self, request, wait):
            raise OperationNotStartedError("connection lost")

    first = LateDecline(Remote())
    fallback = Channel(Remote([("exit", 0)]))
    host = PosixHost(
        executor_providers=(
            ExecutorProvider("first", first),
            ExecutorProvider("second", fallback),
        )
    )

    with pytest.raises(ConnectionError, match="already sent") as caught:
        host.run("rm -rf /var/cache/app")

    assert not isinstance(caught.value, OperationNotStartedError)
    assert isinstance(caught.value.__cause__, OperationNotStartedError)
    assert first.abandoned == 1
    assert fallback.remote.sent == []


def test_a_connection_lost_mid_command_stops_it_and_propagates():
    remote = Remote([("out", b"partial")], drop_after=1)
    executor = Channel(remote)

    with pytest.raises(ConnectionResetError):
        executor("cmd")

    assert executor.abandoned == 1


def test_an_interrupt_stops_the_command():
    class Interrupted(Channel):
        def _receive(self, request, wait):
            raise KeyboardInterrupt

    executor = Interrupted(Remote())
    with pytest.raises(KeyboardInterrupt):
        executor("cmd")

    assert executor.abandoned == 1


def test_a_streaming_executor_honours_the_same_call_contract():
    remote = Remote([("out", b"x"), ("exit", 1)])

    with pytest.raises(subprocess.CalledProcessError) as caught:
        Channel(remote)("cmd", stdin=io.StringIO("typed"))

    assert remote.sent == [("cmd", b"typed")]
    assert caught.value.stdout == b"x"


def test_request_is_immutable():
    request = ExecutorRequest("cmd")

    with pytest.raises(AttributeError):
        request.command = "other"  # type: ignore[misc]


# -- the guide's worked example ---------------------------------------------

_GUIDE = Path(__file__).resolve().parents[1] / "docs" / "guide" / "extending.md"


def test_the_guide_s_worked_executor_runs():
    text = _GUIDE.read_text(encoding="utf-8")
    section = text.split("## Writing an executor", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```python\n(.*?)```", section, re.S)

    assert len(blocks) == 2, "the guide's worked executor changed shape"
    namespace: dict = {"__name__": "guide_example"}
    exec(compile("\n".join(blocks), str(_GUIDE), "exec"), namespace)
