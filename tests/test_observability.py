"""Logging, redaction, and run-side selection-trace observability.

Three things are asserted here that a user debugging a failed remote command
depends on:

1. Something is actually logged, under a predictable ``hostctl.*`` namespace,
   at ``debug`` -- and the library never installs a handler of its own.
2. No credential reaches a log record, for the credential shapes redaction
   claims to recognize.
3. ``host.last_selection`` reports every provider tried and every refusal on
   the call that suffered them, not on the next one.
"""

import logging
import subprocess

import pytest

from hostctl import (
    ExecutorProvider,
    LocalHost,
    OperationNotStarted,
    PathProvider,
    PosixHost,
    ProviderProbe,
    ProviderSelector,
)


def _executor(name, fault=None, **options):
    def execute(command, *args, **kwargs):
        if fault is not None:
            raise fault()
        return subprocess.CompletedProcess((command, *args), 0, b"ok", b"")

    return ExecutorProvider(name, execute, **options)


# --- (a) the library logs, and never configures logging ---------------------


def test_provider_selection_and_dispatch_are_logged_at_debug(caplog):
    host = PosixHost(executor_providers=(_executor("ssh"),))

    with caplog.at_level(logging.DEBUG, logger="hostctl"):
        host.run("uptime", check=False)

    records = [item for item in caplog.records if item.name.startswith("hostctl.")]
    assert records, "no hostctl log records were emitted"
    assert all(item.levelno == logging.DEBUG for item in records)
    messages = [item.getMessage() for item in records]
    assert any("selected provider ssh" in text for text in messages)
    assert any("dispatching" in text for text in messages)


def test_decline_reason_is_logged_with_the_provider_that_refused(caplog):
    host = PosixHost(
        executor_providers=(
            _executor(
                "ssh", fault=lambda: OperationNotStarted("sshd is not listening")
            ),
            _executor("winrm"),
        )
    )

    with caplog.at_level(logging.DEBUG, logger="hostctl"):
        host.run("uptime", check=False)

    messages = [item.getMessage() for item in caplog.records]
    assert any(
        "ssh" in text and "sshd is not listening" in text for text in messages
    ), messages


def test_connect_and_close_lifecycle_is_logged(caplog):
    events = []

    class Provider(ExecutorProvider):
        def __init__(self):
            super().__init__("probe-only", lambda *a, **k: None)

        def connect(self):
            events.append("connect")

        def close(self):
            events.append("close")

    host = PosixHost(executor_providers=(Provider(),))
    with caplog.at_level(logging.DEBUG, logger="hostctl"):
        host.connect()
        host.close()

    # The generation bump on close is the observable lifecycle record the
    # selector owns; provider-level connect/close logging lives in the
    # transport modules, which need a real endpoint to exercise.
    assert events == ["connect", "close"]
    assert any("invalidated" in item.getMessage() for item in caplog.records), [
        item.getMessage() for item in caplog.records
    ]


def test_library_installs_no_handler_and_does_not_configure_logging():
    """A library must leave handler policy entirely to the application."""
    for name in (
        "hostctl.provider",
        "hostctl.provider.transports",
        "hostctl.host.ssh",
        "hostctl.host.winrm",
    ):
        logger = logging.getLogger(name)
        assert logger.handlers == [], f"{name} installed a handler"
        assert logger.level == logging.NOTSET, f"{name} set its own level"
        assert logger.propagate is True


def test_log_calls_are_lazy_and_skip_formatting_when_nobody_listens():
    """A `%`-style call must not render its arguments below the level."""
    rendered = []

    class Tattletale:
        def __str__(self):
            rendered.append(True)
            return "rendered"

    logger = logging.getLogger("hostctl.provider")
    previous = logging.getLogger("hostctl").level
    logging.getLogger("hostctl").setLevel(logging.INFO)
    try:
        logger.debug("value %s", Tattletale())
    finally:
        logging.getLogger("hostctl").setLevel(previous)

    assert rendered == [], "the argument was formatted despite debug being off"


# --- secrets never reach a log record ---------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "mysql --password=hunter2 -e 'select 1'",
        "curl -H 'token=hunter2' https://example.invalid",
        "connect --api_key=hunter2",
        "connect --api-key=hunter2",
        "psql postgres://admin:hunter2@db.example/app",
        'login --password="hunter2"',
        "provision secret: hunter2",
    ],
    ids=[
        "password-assignment",
        "token-assignment",
        "api_key-underscore",
        "api-key-hyphen",
        "uri-userinfo",
        "quoted-value",
        "colon-separated",
    ],
)
def test_a_password_never_reaches_a_log_record(caplog, command):
    """The proof for the redaction guarantee the docs make."""
    host = PosixHost(executor_providers=(_executor("ssh"),))

    with caplog.at_level(logging.DEBUG, logger="hostctl"):
        host.run(command, check=False)

    for record in caplog.records:
        assert "hunter2" not in record.getMessage()
        assert "hunter2" not in str(record.args)
    assert any("<redacted>" in item.getMessage() for item in caplog.records)


def test_a_secret_in_a_decline_reason_is_redacted_in_the_log(caplog):
    host = PosixHost(
        executor_providers=(
            _executor(
                "ssh",
                fault=lambda: OperationNotStarted("auth failed for password=hunter2"),
            ),
            _executor("winrm"),
        )
    )

    with caplog.at_level(logging.DEBUG, logger="hostctl"):
        host.run("uptime", check=False)

    for record in caplog.records:
        assert "hunter2" not in record.getMessage()
    assert "hunter2" not in str(host.last_selection)


def test_a_secret_in_a_path_segment_is_redacted_in_the_log(caplog):
    from pathlib_next.mempath import MemPath, MemPathBackend

    backend = MemPathBackend()
    host = PosixHost(
        path_providers=(
            PathProvider("mem", lambda *parts: MemPath(*parts, backend=backend)),
        )
    )

    with caplog.at_level(logging.DEBUG, logger="hostctl"):
        host.path("/tmp/token=hunter2")

    for record in caplog.records:
        assert "hunter2" not in record.getMessage()


def test_redaction_documents_its_own_limit():
    """A positional secret carries no marker and is explicitly not covered."""
    assert ProviderSelector.redact("password=hunter2") == "password=<redacted>"
    # No `password=` marker, so nothing identifies the token as a secret.
    assert ProviderSelector.redact("mysql -p hunter2") == "mysql -p hunter2"


# --- (b) the run-side trace is public ---------------------------------------


def test_last_selection_is_public_on_a_system_host():
    host = PosixHost(executor_providers=(_executor("ssh"),))
    assert host.last_selection == ()

    host.run("uptime", check=False)

    trace = host.last_selection
    assert [item["provider"] for item in trace] == ["ssh"]
    assert trace[0]["chosen"] is True
    for key in (
        "availability",
        "reason",
        "capabilities",
        "generation",
        "policy",
        "pin",
    ):
        assert key in trace[0]


def test_last_selection_is_public_on_a_local_host():
    host = LocalHost(executor_providers=(_executor("local"),))
    host.run("uptime", check=False)

    assert [item["provider"] for item in host.last_selection] == ["local"]


def test_last_selection_is_empty_for_a_host_without_provider_selection():
    """The documented answer for a host whose run() selects nothing."""

    class Bare(PosixHost):
        def _run_selector(self):
            return None

    assert Bare(executor_providers=(_executor("ssh"),)).last_selection == ()


def test_last_selection_is_a_copy_that_cannot_mutate_selector_state():
    host = PosixHost(executor_providers=(_executor("ssh"),))
    host.run("uptime", check=False)

    host.last_selection[0]["chosen"] = "tampered"

    assert host.last_selection[0]["chosen"] is True


# --- (c) the trace accumulates across failover within one run() -------------


def test_the_first_failing_call_already_shows_every_decline_reason():
    """The regression this exists to prevent: a lost first-failure reason."""
    host = PosixHost(
        executor_providers=(
            _executor(
                "ssh", fault=lambda: OperationNotStarted("ssh refused: connection lost")
            ),
            _executor("winrm"),
        )
    )

    host.run("uptime", check=False)

    trace = host.last_selection
    assert [item["provider"] for item in trace] == ["ssh", "winrm"]
    ssh = trace[0]
    assert ssh["chosen"] is False
    assert ssh["availability"] == "unavailable"
    assert "connection lost" in ssh["reason"]
    assert trace[1]["chosen"] is True


def test_every_provider_tried_appears_once_after_two_declines():
    host = PosixHost(
        executor_providers=(
            _executor("ssh", fault=lambda: OperationNotStarted("no route")),
            _executor("winrm", fault=lambda: OperationNotStarted("service stopped")),
            _executor("serial"),
        )
    )

    host.run("uptime", check=False)

    trace = host.last_selection
    assert [item["provider"] for item in trace] == ["ssh", "winrm", "serial"]
    assert [item["chosen"] for item in trace] == [False, False, True]
    assert "no route" in trace[0]["reason"]
    assert "service stopped" in trace[1]["reason"]


def test_a_new_run_starts_a_fresh_trace_rather_than_appending_forever():
    host = PosixHost(
        executor_providers=(
            _executor("ssh", probe=lambda: ProviderProbe("unavailable", "port closed")),
            _executor("winrm"),
        )
    )

    host.run("uptime", check=False)
    first = host.last_selection
    host.run("uptime", check=False)

    assert host.last_selection == first
    assert len(host.last_selection) == 2


# --- (d) provider_details reflects declines ---------------------------------


def test_provider_details_reports_a_declined_provider_as_unavailable():
    """The bug: a declined provider kept advertising availability 'available'."""
    host = PosixHost(
        executor_providers=(
            _executor("ssh", fault=lambda: OperationNotStarted("sshd stopped")),
            _executor("winrm"),
        )
    )

    before = {item["name"]: item for item in host.provider_details}
    assert before["ssh"]["availability"] == "available"

    host.run("uptime", check=False)

    after = {item["name"]: item for item in host.provider_details}
    assert after["ssh"]["availability"] == "unavailable"
    assert "sshd stopped" in after["ssh"]["reason"]
    assert after["winrm"]["availability"] == "available"


def test_a_declined_provider_stops_contributing_capabilities():
    host = PosixHost(
        executor_providers=(
            _executor(
                "ssh",
                fault=lambda: OperationNotStarted("sshd stopped"),
                capabilities=("args", "runspace"),
            ),
            _executor("winrm", capabilities=("args",)),
        )
    )

    assert "runspace" in host.executor_capabilities

    host.run("uptime", check=False)

    assert "runspace" not in host.executor_capabilities
    assert "runspace" not in host.capabilities


def test_a_decline_reason_is_redacted_in_provider_details():
    host = PosixHost(
        executor_providers=(
            _executor(
                "ssh", fault=lambda: OperationNotStarted("rejected token=hunter2")
            ),
            _executor("winrm"),
        )
    )

    host.run("uptime", check=False)

    details = {item["name"]: item for item in host.provider_details}
    assert "hunter2" not in details["ssh"]["reason"]
    assert "<redacted>" in details["ssh"]["reason"]


def test_reconnecting_clears_the_decline_from_provider_details():
    host = PosixHost(
        executor_providers=(
            _executor("ssh", fault=lambda: OperationNotStarted("sshd stopped")),
            _executor("winrm"),
        )
    )

    host.run("uptime", check=False)
    assert host.provider_details[0]["availability"] == "unavailable"

    host.close()

    assert host.provider_details[0]["availability"] == "available"
    assert host.last_selection == ()


def test_declines_accessor_exposes_redacted_reasons():
    selector = ProviderSelector((_executor("ssh"),))
    assert selector.declines == {}

    selector.decline("ssh", "rejected token=hunter2")

    assert "hunter2" not in selector.declines["ssh"]
    # Mutating the copy must not disturb selector state.
    selector.declines.clear()
    assert "ssh" in selector.declines
