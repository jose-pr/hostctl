"""`hostctl run`/`info` over several hosts; one host behaves exactly as before.

Hosts are fakes keyed by URI (patched in as `hostctl.cli._common.Host`), so
each test decides what every host answers, including failing to connect.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

import duho.mcp as duho_mcp
import pytest

from hostctl.cli import main
from hostctl.cli._fanout import expand_targets
from hostctl.cli._root import Hostctl
from hostctl.cli._status import worst
from hostctl.host._info import HostInfo


class _Host:
    """Answers `run` with `reply` (a `(stdout, stderr, status)` or an
    exception) and `_collect_info` with a `HostInfo` naming the host."""

    def __init__(self, uri, reply):
        self.uri = uri
        self.reply = reply

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, command, **options):
        if callable(self.reply):
            self.reply = self.reply()
        if isinstance(self.reply, BaseException):
            raise self.reply
        out, err, code = self.reply
        return subprocess.CompletedProcess(command, code, out, err)

    def _collect_info(self):
        if isinstance(self.reply, BaseException):
            return HostInfo(), False, [self.reply]
        return HostInfo(hostname=self.uri.rpartition("/")[2]), True, []


@pytest.fixture
def hosts(monkeypatch):
    """`hosts[uri] = reply`; a URI not listed answers `(b"", b"", 0)`."""
    replies = {}
    opened = []

    def open_host(uri, **credentials):
        opened.append((uri, credentials))
        reply = replies.get(uri, (b"", b"", 0))
        if isinstance(reply, ConnectionError):
            raise reply
        return _Host(uri, reply)

    monkeypatch.setattr("hostctl.cli._common.Host", open_host)
    replies["opened"] = opened
    return replies


# -- naming several hosts ---------------------------------------------------


@pytest.mark.parametrize(
    "operand, expected",
    [
        ("local:", ["local:"]),
        ("ssh://nas1,nas2", ["ssh://nas1", "ssh://nas2"]),
        ("ssh://admin@nas1,nas2:2222", ["ssh://admin@nas1", "ssh://admin@nas2:2222"]),
        # The user part is credentials: a comma there separates nothing.
        ("ssh://u:pa,ss@nas1,nas2", ["ssh://u:pa,ss@nas1", "ssh://u:pa,ss@nas2"]),
        ("ssh://u:p[1-2]@h", ["ssh://u:p[1-2]@h"]),
        ("ssh://nas[1-3]", ["ssh://nas1", "ssh://nas2", "ssh://nas3"]),
        ("ssh://n[1-2:02d]", ["ssh://n01", "ssh://n02"]),
        (
            "ssh://a@h1,docker://web,local:",
            ["ssh://a@h1", "docker://web", "local:"],
        ),
        ("ssh://a@h1,ssh://b@h2,h3", ["ssh://a@h1", "ssh://b@h2", "ssh://b@h3"]),
        ("ssh://[::1],[::2]:2222", ["ssh://[::1]", "ssh://[::2]:2222"]),
        (
            "serial:///dev/ttyUSB[0-1]",
            ["serial:///dev/ttyUSB0", "serial:///dev/ttyUSB1"],
        ),
        ("local:,,", ["local:"]),
    ],
)
def test_an_operand_names_its_hosts(operand, expected):
    assert expand_targets([operand]) == expected


def test_operands_are_taken_in_order():
    assert expand_targets(["ssh://a,b", "local:"]) == ["ssh://a", "ssh://b", "local:"]


def test_a_malformed_range_is_a_usage_failure(capsys):
    assert main(["run", "ssh://nas[3-1]", "--", "true"]) == 125
    assert "reversed range" in capsys.readouterr().err


# -- the status of several hosts -------------------------------------------


@pytest.mark.parametrize(
    "codes, expected",
    [
        ([], 0),
        ([0, 0], 0),
        ([0, 3, 1], 3),
        ([0, -9, 2], -9),
        ([255, 125], 125),
        ([125, 126, 3], 126),
        ([127, 130, 1], 130),
    ],
)
def test_the_worst_status_wins(codes, expected):
    assert worst(codes) == expected


# -- one host: unchanged ----------------------------------------------------


def test_one_host_is_unprefixed_whatever_the_options(hosts, capsysbinary):
    hosts["ssh://nas"] = (b"out\n", b"err\n", 7)

    assert main(["run", "ssh://nas", "--parallel", "4", "--", "x"]) == 7

    captured = capsysbinary.readouterr()
    assert (captured.out, captured.err) == (b"out\n", b"err\n")


def test_one_host_after_expansion_is_still_one_host(hosts, capsysbinary):
    hosts["ssh://nas"] = (b"out", b"", 0)

    assert main(["run", "ssh://nas,", "--", "x"]) == 0

    assert capsysbinary.readouterr().out == b"out"


def test_one_host_info_is_the_bare_identity(hosts, capsys):
    assert main(["info", "ssh://nas"]) == 0

    assert json.loads(capsys.readouterr().out)["hostname"] == "nas"


def test_one_failing_host_keeps_its_one_line_report(hosts, capsys):
    hosts["ssh://nas"] = ConnectionError("offline")

    assert main(["run", "ssh://nas", "--", "x"]) == 125

    assert capsys.readouterr().err == "hostctl: offline\n"


def test_the_uri_is_still_a_string_to_a_tool_caller():
    tools = {tool["name"]: tool for tool in duho_mcp.describe_tools(Hostctl)}
    for name in ("hostctl.run", "hostctl.info"):
        properties = tools[name]["inputSchema"]["properties"]
        assert properties["uri"]["type"] == "string"
        assert properties["more"]["type"] == "array"
        assert properties["parallel"]["type"] == "integer"


# -- several hosts ----------------------------------------------------------


def test_every_record_names_its_host_and_failures_are_reported(hosts, capsysbinary):
    hosts["ssh://a"] = (b"one\ntwo\n", b"", 0)
    hosts["ssh://b"] = (b"three", b"warn\r\n", 3)
    hosts["ssh://c"] = PermissionError("auth failed")

    code = main(["run", "ssh://a,b", "ssh://c", "--", "x"])

    captured = capsysbinary.readouterr()
    assert code == 126
    assert captured.out == b"[ssh://a] one\n[ssh://a] two\n[ssh://b] three\n"
    assert captured.err.splitlines() == [
        b"[ssh://b] warn",
        b"hostctl: [ssh://b] exit status 3",
        b"hostctl: [ssh://c] exit status 126: permission denied: auth failed",
    ]


def test_an_unreachable_host_does_not_stop_the_others(hosts, capsys):
    hosts["ssh://down"] = ConnectionError("no route to host")
    hosts["ssh://up"] = ("fine\n", "", 0)

    assert main(["run", "ssh://down,up", "--", "x"]) == 125

    captured = capsys.readouterr()
    assert captured.out == "[ssh://up] fine\n"
    assert captured.err == "hostctl: [ssh://down] exit status 125: no route to host\n"


def test_an_unexpected_error_is_status_1_naming_its_type(hosts, capsys):
    hosts["ssh://a"] = RuntimeError("broken")

    assert main(["run", "ssh://a,b", "--", "x"]) == 1

    assert "hostctl: [ssh://a] exit status 1: RuntimeError: broken" in (
        capsys.readouterr().err
    )


def test_a_password_never_reaches_a_label(hosts, capsys):
    hosts["ssh://u:hunter2@h1"] = ("x\n", "", 4)

    main(["run", "ssh://u:hunter2@h1,h2", "--", "x"])

    captured = capsys.readouterr()
    assert "hunter2" not in captured.out + captured.err
    assert "[ssh://u@h1] x" in captured.out
    assert [uri for uri, _ in hosts["opened"]] == [
        "ssh://u:hunter2@h1",
        "ssh://u:hunter2@h2",
    ]


def test_a_password_is_asked_for_once(hosts, monkeypatch):
    asked = []
    monkeypatch.setattr("getpass.getpass", lambda prompt: asked.append(1) or "pw")

    main(["run", "ssh://a,b,c", "--ask-password", "--", "x"])

    assert asked == [1]
    assert [cred for _, cred in hosts["opened"]] == [{"password": "pw"}] * 3


def test_hosts_run_concurrently_up_to_parallel(hosts, capsys):
    barrier = threading.Barrier(3, timeout=10)

    def meet():
        barrier.wait()
        return ("met\n", "", 0)

    for name in "abc":
        hosts[f"ssh://{name}"] = meet

    assert main(["run", "ssh://a,b,c", "--parallel", "3", "--", "x"]) == 0

    assert sorted(capsys.readouterr().out.splitlines()) == [
        "[ssh://a] met",
        "[ssh://b] met",
        "[ssh://c] met",
    ]


def test_hosts_run_in_order_one_at_a_time_by_default(hosts, capsys):
    main(["run", "ssh://h[1-4]", "--", "x"])
    assert [uri for uri, _ in hosts["opened"]] == [f"ssh://h{n}" for n in range(1, 5)]


def test_parallel_must_be_positive(hosts, capsys):
    assert main(["run", "ssh://a,b", "--parallel", "0", "--", "x"]) == 125
    assert "--parallel must be at least 1" in capsys.readouterr().err


def test_an_interrupt_is_not_swallowed_as_a_host_failure(hosts):
    """As with one host, Ctrl-C ends the command; duho.fanout cancels the
    hosts still queued when it reaches the waiting main thread."""
    hosts["ssh://b"] = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        main(["run", "ssh://a,b,c", "--", "x"])


def test_info_writes_one_record_per_host(hosts, capsys):
    hosts["ssh://down"] = OSError("refused")

    code = main(["info", "ssh://n1", "ssh://down", "ssh://n2"])

    captured = capsys.readouterr()
    assert code == 125
    records = [json.loads(line) for line in captured.out.splitlines()]
    assert [(r["target"], r["result"]["hostname"]) for r in records] == [
        ("ssh://n1", "n1"),
        ("ssh://n2", "n2"),
    ]
    assert captured.err == "hostctl: [ssh://down] exit status 125: refused\n"


def test_a_tool_call_fans_out_too():
    program = [sys.executable, "-c", "print('hi')"]
    result = duho_mcp.call_tool(
        Hostctl,
        "hostctl.run",
        {"uri": "local:,local:", "more": ["local:"], "--": program},
    )

    assert not result.get("isError"), result
    assert result["content"][0]["text"].splitlines() == ["[local:] hi"] * 3


def test_a_real_local_fan_out_keeps_each_status(capsysbinary):
    program = "import sys; print(sys.argv[1]); sys.exit(int(sys.argv[1]))"
    code = main(["run", "local:,local:", "--", sys.executable, "-c", program, "5"])

    assert code == 5
    captured = capsysbinary.readouterr()
    assert captured.out.splitlines() == [b"[local:] 5", b"[local:] 5"]
    assert captured.err.splitlines() == [b"hostctl: [local:] exit status 5"] * 2
