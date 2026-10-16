"""Every config spells its timeouts as one `Timeout`, and `run()` resolves it.

The table here is the contract the API header documents: which fields each
transport honours, what `DEFAULT` becomes, and how `run(timeout=)` picks its
deadline when the call leaves it out.
"""

from __future__ import annotations

import dataclasses
import inspect
import subprocess
import time
import typing
from urllib.parse import parse_qs, urlsplit

import pytest

from hostctl import (
    DEFAULT,
    ContainerConfig,
    ExecutorProvider,
    HostConfig,
    IOSConfig,
    LocalConfig,
    PosixConfig,
    PosixHost,
    QEMUConfig,
    SerialConfig,
    SSHConfig,
    Timeout,
    WindowsConfig,
    WindowsHost,
    WinRMConfig,
)
from hostctl.transports.container._host import ContainerHost
from hostctl.transports.qemu._host import QEMUHost
from hostctl.transports.serial._host import SerialHost

_UNIX = {"transport": "unix", "socket_path": "/run/qemu-server/vm.qga"}


def _qga_ssh(**options):
    return QEMUConfig(
        "vm", transport="ssh", ssh=SSHConfig("pve", username="root"), **options
    )


# --- what DEFAULT resolves to -------------------------------------------------

_RESOLVED = [
    (lambda: LocalConfig(), Timeout(None)),
    (lambda: SSHConfig("h"), Timeout(None, connect=30)),
    (lambda: WinRMConfig("h", username="u"), Timeout(30, connect=30)),
    (lambda: ContainerConfig("c"), Timeout(connect=60)),
    (lambda: QEMUConfig("vm"), Timeout(None, read=10)),
    (lambda: QEMUConfig("vm", **_UNIX), Timeout(None, connect=10, read=10)),
    (lambda: _qga_ssh(), Timeout(None, connect=10, read=10)),
    (
        lambda: QEMUConfig("vm", timeout=Timeout(read=3), **_UNIX),
        Timeout(None, connect=3, read=3),
    ),
    (
        lambda: QEMUConfig("vm", timeout=Timeout(connect=1), **_UNIX),
        Timeout(None, connect=1, read=10),
    ),
    (lambda: SerialConfig("loop://"), Timeout(None, read=0.1, write=10)),
    (lambda: PosixConfig("nas"), Timeout()),
    (lambda: WindowsConfig("win"), Timeout()),
    (lambda: IOSConfig("switch"), Timeout()),
    (lambda: PosixConfig("nas", timeout=5), Timeout(5)),
    (
        lambda: SSHConfig("h", timeout=Timeout(60, connect=None)),
        Timeout(60, connect=None),
    ),
]


@pytest.mark.parametrize(
    "factory,expected", _RESOLVED, ids=[str(i) for i in range(len(_RESOLVED))]
)
def test_timeouts_resolves_each_default_the_transport_supports(factory, expected):
    assert factory().timeouts == expected


_CONFIGS = {
    "LocalConfig": lambda **o: LocalConfig(**o),
    "SSHConfig": lambda **o: SSHConfig("h", **o),
    "WinRMConfig": lambda **o: WinRMConfig("h", username="u", **o),
    "ContainerConfig": lambda **o: ContainerConfig("c", **o),
    "QEMUConfig": lambda **o: QEMUConfig("vm", **_UNIX, **o),
    "SerialConfig": lambda **o: SerialConfig("loop://", **o),
    "PosixConfig": lambda **o: PosixConfig("nas", **o),
    "WindowsConfig": lambda **o: WindowsConfig("win", **o),
    "IOSConfig": lambda **o: IOSConfig("switch", **o),
}

_WITH_TOTAL = sorted(set(_CONFIGS) - {"ContainerConfig"})


@pytest.mark.parametrize("name", sorted(_CONFIGS))
def test_the_stored_value_defaults_to_an_all_default_timeout(name):
    config = _CONFIGS[name]()

    assert type(config.timeout) is Timeout
    assert config.timeout == Timeout()


@pytest.mark.parametrize("name", _WITH_TOTAL)
@pytest.mark.parametrize("value", [60, 2.5, None])
def test_a_number_or_none_is_stored_as_the_total(name, value):
    config = _CONFIGS[name](timeout=value)

    assert config.timeout == Timeout(total=value)
    assert config.timeouts.total == (None if value is None else float(value))


@pytest.mark.parametrize("name", sorted(_CONFIGS))
def test_a_timeout_value_is_stored_as_given(name):
    timeout = Timeout()

    assert _CONFIGS[name](timeout=timeout).timeout is timeout


@pytest.mark.parametrize("name", sorted(_CONFIGS))
@pytest.mark.parametrize("value", ["30", True, 0, -1])
def test_a_bad_timeout_argument_raises(name, value):
    with pytest.raises((TypeError, ValueError)):
        _CONFIGS[name](timeout=value)


@pytest.mark.parametrize("name", sorted(_CONFIGS))
def test_the_signature_takes_a_number_none_or_timeout(name):
    cls = type(_CONFIGS[name]())
    parameter = inspect.signature(cls).parameters["timeout"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == Timeout()
    hints = typing.get_type_hints(cls.__init__)
    assert hints["timeout"] == typing.Union[float, None, Timeout]


# --- fields a transport cannot honour -----------------------------------------

_UNSUPPORTED = [
    ("LocalConfig", "connect"),
    ("LocalConfig", "read"),
    ("LocalConfig", "write"),
    ("SSHConfig", "read"),
    ("SSHConfig", "write"),
    ("WinRMConfig", "read"),
    ("WinRMConfig", "write"),
    ("ContainerConfig", "total"),
    ("ContainerConfig", "read"),
    ("ContainerConfig", "write"),
    ("QEMUConfig", "write"),
    ("SerialConfig", "connect"),
    ("PosixConfig", "connect"),
    ("PosixConfig", "read"),
    ("PosixConfig", "write"),
    ("WindowsConfig", "connect"),
    ("IOSConfig", "read"),
]


@pytest.mark.parametrize("name,field", _UNSUPPORTED)
@pytest.mark.parametrize("value", [5, None])
def test_an_unsupported_field_raises_naming_the_config_and_field(name, field, value):
    with pytest.raises(ValueError, match=f"^{name}: timeout.{field} is not supported"):
        _CONFIGS[name](timeout=Timeout(**{field: value}))


@pytest.mark.parametrize("name,field", _UNSUPPORTED)
def test_an_unsupported_field_left_default_is_accepted(name, field):
    assert _CONFIGS[name](timeout=Timeout(**{field: DEFAULT})).timeout == Timeout()


def test_a_container_refuses_a_total_given_as_a_number():
    with pytest.raises(ValueError, match="ContainerConfig: timeout.total"):
        ContainerConfig("c", timeout=30)


def test_qemu_refuses_connect_where_hostctl_does_not_open_the_stream():
    with pytest.raises(ValueError, match="transport='libvirt'"):
        QEMUConfig("vm", timeout=Timeout(connect=5))
    with pytest.raises(ValueError, match="transport_factory"):
        QEMUConfig(
            "vm",
            timeout=Timeout(connect=5),
            transport_factory=lambda: None,
            **_UNIX,
        )
    assert QEMUConfig("vm", transport_factory=lambda: None, **_UNIX).timeouts == (
        Timeout(None, read=10)
    )


def test_assignment_and_replace_coerce_and_validate():
    config = SSHConfig("h")

    config.timeout = 7  # type: ignore[assignment]
    assert config.timeout == Timeout(7)
    assert dataclasses.replace(config, timeout=None).timeout == Timeout(None)
    assert dataclasses.replace(config).timeout == Timeout(7)
    with pytest.raises(ValueError, match="SSHConfig: timeout.write"):
        dataclasses.replace(config, timeout=Timeout(write=1))


def test_configs_differing_only_in_timeout_are_unequal():
    assert SSHConfig("h", timeout=5) == SSHConfig("h", timeout=Timeout(5.0))
    assert SSHConfig("h", timeout=5) != SSHConfig("h")


def test_the_repr_shows_the_normalised_value():
    assert "timeout=Timeout(total=60.0)" in repr(SSHConfig("h", timeout=60))
    assert "timeout=Timeout()" in repr(SerialConfig("loop://"))


@pytest.mark.parametrize(
    "factory,name",
    [
        (lambda: SSHConfig("h", connect_timeout=5), "connect_timeout"),
        (lambda: WinRMConfig("h", username="u", connect_timeout=5), "connect_timeout"),
        (lambda: QEMUConfig("vm", agent_timeout=5), "agent_timeout"),
        (lambda: SerialConfig("loop://", read_timeout=1), "read_timeout"),
        (lambda: SerialConfig("loop://", write_timeout=1), "write_timeout"),
    ],
)
def test_the_removed_fields_are_not_keywords(factory, name):
    with pytest.raises(TypeError, match=name):
        factory()


def test_serial_keeps_inter_byte_timeout_as_its_own_field():
    config = SerialConfig("loop://", inter_byte_timeout=0.05)

    assert config.settings.inter_byte_timeout == 0.05
    assert config.timeouts == Timeout(None, read=0.1, write=10)


def test_serial_settings_carry_the_resolved_read_and_write():
    config = SerialConfig("loop://", timeout=Timeout(read=None, write=2))
    options = config.settings.factory_options()

    assert (options["timeout"], options["write_timeout"]) == (None, 2.0)


def test_serial_zero_read_and_write_reach_pyserial_as_do_not_wait():
    config = SerialConfig("loop://", timeout=Timeout(read=0, write=0))
    options = config.settings.factory_options()

    assert (options["timeout"], options["write_timeout"]) == (0.0, 0.0)
    assert config.timeouts == Timeout(None, read=0, write=0)
    again = HostConfig(config.connection_uri)
    assert "timeout_read=0" in config.connection_uri
    assert again.timeout == Timeout(read=0, write=0)


def test_a_serial_read_with_a_zero_port_timeout_does_not_block():
    """pyserial's own `loop://` port, so `timeout=0` is the driver's."""
    pytest.importorskip("serial", reason="pyserial is not installed")
    host = SerialHost(SerialConfig("loop://", timeout=Timeout(read=0)))
    with host.spawn() as session:
        started = time.monotonic()
        assert session.read(16) == b""
        session.write(b"ping")
        assert session.read(16) == b"ping"
    assert time.monotonic() - started < 1


@pytest.mark.parametrize(
    "name, make",
    [
        ("QEMUConfig", lambda: QEMUConfig("vm", **_UNIX, timeout=Timeout(read=0))),
        (
            "QEMUConfig",
            lambda: HostConfig("qga+unix:///s?domain=vm&timeout_read=0"),
        ),
    ],
)
def test_a_transport_that_cannot_return_at_once_refuses_zero(name, make):
    with pytest.raises(ValueError, match=rf"{name}: timeout.read=0"):
        make()


@pytest.mark.parametrize("field", ["read", "write"])
def test_a_transport_without_the_field_still_names_it_for_zero(field):
    with pytest.raises(ValueError, match=f"timeout.{field} is not supported"):
        SSHConfig("h", timeout=Timeout(**{field: 0}))


def test_ssh_connect_reaches_asyncssh_and_none_leaves_its_default():
    assert SSHConfig("h").connect_opts()["connect_timeout"] == 30.0
    assert (
        SSHConfig("h", timeout=Timeout(connect=4)).connect_opts()["connect_timeout"]
        == 4.0
    )
    assert "connect_timeout" not in (
        SSHConfig("h", timeout=Timeout(connect=None)).connect_opts()
    )


# --- URI keys -----------------------------------------------------------------

_ROUND_TRIPS = [
    lambda: LocalConfig(timeout=5),
    lambda: SSHConfig("h", timeout=Timeout(60, connect=None)),
    lambda: WinRMConfig("h", username="u", timeout=Timeout(None, connect=2.5)),
    lambda: ContainerConfig("c", timeout=Timeout(connect=None)),
    lambda: QEMUConfig("vm", timeout=Timeout(120, read=None)),
    lambda: QEMUConfig("vm", timeout=Timeout(connect=1, read=2), **_UNIX),
    lambda: _qga_ssh(timeout=Timeout(read=4)),
    lambda: SerialConfig("loop://", timeout=Timeout(5, read=None, write=1)),
    lambda: PosixConfig("nas", executor=("local",), timeout=9),
    lambda: WindowsConfig("win", timeout=None),
    lambda: IOSConfig("switch", timeout=0.5),
]


@pytest.mark.parametrize(
    "factory", _ROUND_TRIPS, ids=[str(i) for i in range(len(_ROUND_TRIPS))]
)
def test_a_timeout_round_trips_through_every_scheme(factory):
    config = factory()
    restored = HostConfig(config.connection_uri)

    assert type(restored) is type(config)
    assert restored.timeout == config.timeout
    assert restored.connection_uri == config.connection_uri


@pytest.mark.parametrize("name", sorted(_CONFIGS))
def test_a_default_timeout_puts_no_key_in_the_uri(name):
    query = parse_qs(urlsplit(_CONFIGS[name]().connection_uri).query)

    assert not {"timeout", "timeout_connect", "timeout_read", "timeout_write"} & set(
        query
    )


def test_the_uri_keys_spell_each_field_and_none_is_empty():
    uri = SerialConfig("loop://", timeout=Timeout(5, read=None, write=1)).connection_uri
    query = parse_qs(urlsplit(uri).query, keep_blank_values=True)

    assert query["timeout"] == ["5"]
    assert query["timeout_read"] == [""]
    assert query["timeout_write"] == ["1"]


@pytest.mark.parametrize(
    "uri,old,new",
    [
        ("ssh://h?connect_timeout=5", "connect_timeout", "timeout_connect"),
        ("winrm://u@h?connect_timeout=5", "connect_timeout", "timeout_connect"),
        ("qemu+libvirt:///vm?agent_timeout=5", "agent_timeout", "timeout_read"),
        ("serial:///dev/ttyS0?read_timeout=1", "read_timeout", "timeout_read"),
        ("serial:///dev/ttyS0?write_timeout=1", "write_timeout", "timeout_write"),
    ],
)
def test_a_removed_uri_key_names_its_replacement(uri, old, new):
    with pytest.raises(
        ValueError, match=f"unknown connection parameter: {old}; use {new}"
    ):
        HostConfig(uri)


def test_an_unsupported_uri_key_raises_like_the_keyword():
    with pytest.raises(ValueError, match="SSHConfig: timeout.write is not supported"):
        HostConfig("ssh://h?timeout_write=1")
    with pytest.raises(ValueError, match="ContainerConfig: timeout.total"):
        HostConfig("docker://c?timeout=5")


# --- run(timeout=) resolution -------------------------------------------------


class _Recorder:
    """An executor that records the options each dispatch carried."""

    executor_capabilities = frozenset(("args", "cwd", "env"))

    def __init__(self):
        self.calls = []

    def __call__(self, command, *args, **options):
        self.calls.append(options)
        return subprocess.CompletedProcess([command, *args], 0, b"", b"")


def _composed(config, host_type=PosixHost):
    recorder = _Recorder()
    provider = ExecutorProvider("fake", recorder, capabilities=("args", "cwd", "env"))
    return host_type(config, executor_providers=(provider,)), recorder


_COMPOSED = [
    (lambda **o: PosixConfig("nas", **o), PosixHost),
    (lambda **o: LocalConfig(**o), PosixHost),
    (lambda **o: SSHConfig("h", **o), PosixHost),
    (lambda **o: WinRMConfig("h", username="u", **o), WindowsHost),
]


@pytest.mark.parametrize("factory,host_type", _COMPOSED)
def test_an_omitted_timeout_with_no_total_leaves_the_provider_its_own(
    factory, host_type
):
    host, recorder = _composed(factory(), host_type)

    host.run("x")
    assert "timeout" not in recorder.calls[-1]


@pytest.mark.parametrize("factory,host_type", _COMPOSED)
def test_an_omitted_timeout_is_the_configured_total(factory, host_type):
    host, recorder = _composed(factory(timeout=5), host_type)

    host.run("x")
    assert recorder.calls[-1]["timeout"] == 5.0


@pytest.mark.parametrize("factory,host_type", _COMPOSED)
def test_a_per_call_timeout_overrides_the_configured_total(factory, host_type):
    host, recorder = _composed(factory(timeout=5), host_type)

    host.run("x", timeout=2)
    host.run("x", timeout=None)
    assert [call["timeout"] for call in recorder.calls] == [2, None]


def test_the_shell_forwards_none_and_leaves_an_omitted_timeout_out():
    host, recorder = _composed(PosixConfig("nas", timeout=5))

    host.shell.run("x")
    host.shell.run("x", timeout=None)
    assert [call.get("timeout", "absent") for call in recorder.calls] == [5.0, None]


def _winrm_host(**options):
    from hostctl.transports.winrm._providers import WinRMExecutorProvider
    from hostctl.transports.winrm._transport import _WinRMTransport

    seen = []

    class _Session:
        timeout = "session default"

        def run_ps(self, script, *, timeout="omitted"):
            seen.append(timeout)
            return type("R", (), {"status_code": 0, "std_out": b"", "std_err": b""})

    config = WinRMConfig("h", username="u", password="p", provider="pywinrm", **options)
    transport = _WinRMTransport(config)
    transport._leg._session = _Session()
    host = WindowsHost(config, executor_providers=(WinRMExecutorProvider(transport),))
    return host, seen


def test_winrm_run_leaves_an_omitted_timeout_to_the_session_default():
    host, seen = _winrm_host()

    host.run("x")
    host.run("x", timeout=None)
    host.run("x", timeout=3)
    assert seen == ["omitted", None, 3]


def test_winrm_run_applies_a_configured_total():
    host, seen = _winrm_host(timeout=12)

    host.run("x")
    assert seen == [12.0]


def test_a_windows_config_total_reaches_a_winrm_provider():
    from hostctl.transports.winrm._providers import WinRMExecutorProvider
    from hostctl.transports.winrm._transport import _WinRMTransport

    seen = []

    class _Session:
        def run_ps(self, script, *, timeout="omitted"):
            seen.append(timeout)
            return type("R", (), {"status_code": 0, "std_out": b"", "std_err": b""})

    transport = _WinRMTransport(
        WinRMConfig("h", username="u", password="p", provider="pywinrm")
    )
    transport._leg._session = _Session()
    provider = WinRMExecutorProvider(transport)
    WindowsHost(WindowsConfig("h"), executor_providers=(provider,)).run("x")
    WindowsHost(WindowsConfig("h", timeout=8), executor_providers=(provider,)).run("x")
    assert seen == ["omitted", 8.0]


class _Container:
    attrs = {"Platform": "linux", "State": {"Running": True}}

    def __init__(self):
        self.calls = []

    def reload(self):
        return None

    def exec_run(self, command, **options):
        self.calls.append(command)
        return type("R", (), {"exit_code": 0, "output": (b"", b"")})


class _Client:
    def __init__(self, container):
        self.containers = type("C", (), {"get": lambda _, name: container})()

    def close(self):
        return None


def _container_host(**options):
    container = _Container()
    made = []

    def factory(**client_options):
        made.append(client_options)
        return _Client(container)

    config = ContainerConfig("c", client_factory=factory, **options)
    return ContainerHost(config), container, made


def test_a_container_run_has_no_total_and_refuses_a_number():
    host, container, _ = _container_host()

    host.run("true")
    host.run("true", timeout=None)
    assert len(container.calls) == 2
    with pytest.raises(NotImplementedError, match="cancellable command timeout"):
        host.run("true", timeout=5)


def test_a_container_connect_reaches_the_sdk_only_when_set():
    host, _, made = _container_host()
    host.client
    configured, _, made_configured = _container_host(timeout=Timeout(connect=12))
    configured.client
    unlimited, _, made_unlimited = _container_host(timeout=Timeout(connect=None))
    unlimited.client

    assert made == [{}]
    assert made_configured == [{"timeout": 12.0}]
    assert made_unlimited == [{"timeout": None}]


class _AgentTransport:
    def execute(self, request, timeout=None):
        command = request["execute"]
        if command == "guest-info":
            return {
                "supported_commands": [
                    {"name": name, "enabled": True}
                    for name in ("guest-exec", "guest-exec-status", "guest-get-osinfo")
                ]
            }
        if command == "guest-get-osinfo":
            return {"id": "ubuntu", "kernel-name": "Linux"}
        return {}

    def close(self):
        return None


def _qemu_host(**options):
    transport = _AgentTransport()
    host = QEMUHost(
        QEMUConfig(
            "vm",
            shell="posix",
            pathname="posix",
            transport_factory=lambda: transport,
            **options,
        )
    )
    seen = []

    def executor(command, *args, **call_options):
        seen.append(call_options["timeout"])
        return subprocess.CompletedProcess([command, *args], 0, b"", b"")

    host._executor = executor
    return host, seen


def test_qemu_run_resolves_omitted_to_the_configured_total():
    host, seen = _qemu_host()
    host.run("true")
    host.run("true", timeout=4)
    configured, seen_configured = _qemu_host(timeout=30)
    configured.run("true")
    configured.run("true", timeout=None)

    assert seen == [None, 4]
    assert seen_configured == [30.0, None]


class _Console:
    is_open = True
    dtr = rts = False
    timeout = write_timeout = None

    def read(self, size=1):
        return b""

    def write(self, data):
        return len(data)

    def flush(self):
        return None

    def close(self):
        self.is_open = False

    def send_break(self, duration=0.25):
        return None


class _Profile:
    can_run = True
    line_terminator = b"\r\n"
    encoding = "utf-8"

    def __init__(self):
        self.seen = []

    def negotiate(self, process):
        return None

    def send(self, process, command):
        return None

    def run(self, process, command, *, timeout=None):
        self.seen.append(timeout)
        return b"", 0


def test_serial_run_resolves_omitted_to_the_configured_total():
    profile = _Profile()
    host = SerialHost(SerialConfig("loop://", profile=profile, serial_port=_Console()))
    host.run("show")
    host.run("show", timeout=60)
    configured = _Profile()
    SerialHost(
        SerialConfig("loop://", profile=configured, serial_port=_Console(), timeout=9)
    ).run("show")

    assert profile.seen[0] is None
    assert 59 < profile.seen[1] <= 60
    assert 8 < configured.seen[0] <= 9


# --- the guest-agent stream ---------------------------------------------------


class _Framed:
    """A framed QGA session whose raw legs record the deadlines they get."""

    def __new__(cls, **options):
        from hostctl.transports.qemu._qga import _QGAFramedSession

        class Session(_QGAFramedSession):
            def __init__(self):
                super().__init__(max_reply_size=1024, **options)
                self.deadlines = []

            def _connect_raw(self, deadline):
                self.deadlines.append(("connect", deadline))

            def _synchronize(self, deadline):
                self.deadlines.append(("sync", deadline))

            def _send(self, payload, deadline):
                self.deadlines.append(("send", deadline))

            def _correlated_reply(self, request_id, deadline):
                return {"return": {}}

            def _close_raw(self):
                return None

        return Session()


def test_an_omitted_connect_shares_the_first_requests_deadline():
    session = _Framed(timeout=10)
    session.execute({"execute": "guest-ping"})

    (_, connect), (_, sync), (_, send) = session.deadlines
    assert connect == sync == send


def test_a_set_connect_bounds_opening_the_stream_separately():
    session = _Framed(timeout=10, connect_timeout=1)
    started = time.monotonic()
    session.execute({"execute": "guest-ping"})

    (_, connect), (_, sync), (_, send) = session.deadlines
    assert connect == sync
    assert connect - started <= 1.5
    assert send - started > 9


def test_a_none_reply_limit_is_no_deadline():
    session = _Framed(timeout=None)
    session.execute({"execute": "guest-ping"})

    assert [deadline for _, deadline in session.deadlines] == [None, None, None]
    assert session._remaining(None) is None


class _LibvirtDomain:
    def isActive(self):
        return True


class _LibvirtConnection:
    def lookupByName(self, name):
        return _LibvirtDomain()

    def close(self):
        return None


def test_libvirt_waits_without_a_limit_for_a_none_reply_limit():
    from hostctl.transports.qemu._qga_transports import LibvirtGuestAgentTransport

    sent = []
    transport = LibvirtGuestAgentTransport(
        "vm",
        timeout=None,
        connect_factory=lambda uri: _LibvirtConnection(),
        command_factory=lambda domain, payload, timeout, flags: (
            sent.append(timeout) or '{"return": {}}'
        ),
    )
    transport.execute({"execute": "guest-ping"})

    assert sent == [-2]
