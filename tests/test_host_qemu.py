"""QemuHost discovery and integration with an injected QGA transport."""

import base64

import pytest

from hostctl import HostConfig, QemuConfig, QemuHost, SshConfig
from hostctl.host.qemu import PosixQemuPath, WindowsQemuPath


class _Transport:
    def __init__(self, *, windows=False):
        self.windows = windows
        self.requests = []
        self.closed = False

    def execute(self, request, timeout=None):
        self.requests.append((request, timeout))
        command = request["execute"]
        if command == "guest-ping":
            return {}
        if command == "guest-info":
            return {
                "supported_commands": [
                    {"name": name, "enabled": True}
                    for name in (
                        "guest-exec",
                        "guest-exec-status",
                        "guest-file-open",
                        "guest-file-read",
                        "guest-file-write",
                        "guest-file-close",
                        "guest-get-osinfo",
                        "guest-get-host-name",
                    )
                ]
            }
        if command == "guest-get-osinfo":
            # What qemu-ga actually sends: `id` is the os-release ID, and
            # `kernel-name` is the family. The fixture used to answer
            # `id: "linux"`, which no real agent returns, and the family
            # assertion below measured nothing.
            return {
                "id": "mswindows" if self.windows else "ubuntu",
                "kernel-name": "Windows" if self.windows else "Linux",
                "pretty-name": (
                    "Microsoft Windows Server 2022"
                    if self.windows
                    else "Ubuntu 24.04.1 LTS"
                ),
                "version": "1",
                "machine": "x86_64",
            }
        if command == "guest-get-host-name":
            return {"host-name": "guest"}
        if command == "guest-exec":
            return {"pid": 42}
        if command == "guest-exec-status":
            return {
                "exited": True,
                "exitcode": 0,
                "out-data": base64.b64encode(b"ok\n").decode(),
            }
        raise AssertionError(command)

    def close(self):
        self.closed = True


def _host(*, windows=False):
    transport = _Transport(windows=windows)
    config = QemuConfig(
        "guest",
        transport="libvirt",
        transport_factory=lambda: transport,
    )
    return QemuHost(config), transport


def test_qemu_host_discovers_capabilities_info_and_posix_path():
    host, transport = _host()

    host.connect()
    assert host.capabilities == frozenset(("path", "run"))
    assert host.info().hostname == "guest"
    assert host.info().os_family == "linux"
    assert host.info().os_name == "Ubuntu 24.04.1 LTS"
    assert isinstance(host.path("/tmp/file"), PosixQemuPath)
    host.close()
    assert transport.closed


def test_qemu_host_selects_windows_shell_and_path():
    host, _ = _host(windows=True)

    assert host.shell_flavour.name == "powershell"
    assert isinstance(host.path(r"C:\Temp"), WindowsQemuPath)


def test_qemu_host_shell_run_embeds_cwd_and_uses_guest_exec():
    host, transport = _host()

    result = host.run(
        ["printf", "%s", "a b"],
        cwd="/tmp/a b",
        env={"NUMBER": 7},
        encoding="utf-8",
    )

    assert result.stdout == "ok\n"
    request = next(
        item for item, _ in transport.requests if item["execute"] == "guest-exec"
    )
    arguments = request["arguments"]
    assert arguments["path"] == "/bin/sh"
    assert "cd -- '/tmp/a b'" in arguments["arg"][-1]
    assert arguments["arg"][-1].count("cd -- ") == 1
    # Embedded in the script, NOT sent as guest-exec's `env` list: qemu-ga
    # passes that list to g_spawn_async_with_pipes as envp, which REPLACES
    # the child environment. `env` is additive on every hostctl transport
    # (docs/guide/contracts.md), so a guest process must keep its PATH.
    assert "env" not in arguments
    assert "NUMBER=7" in arguments["arg"][-1]


def test_direct_qga_argv_refuses_env_rather_than_replacing_it():
    """There is no shell to embed additive assignments into, and QGA's own
    env list would wipe PATH/HOME. Refusing is the honest answer, and it
    matches the rule already applied to cwd."""
    from hostctl import Exec

    host, _ = _host()

    with pytest.raises(NotImplementedError, match="env"):
        host.run(Exec("/usr/bin/id"), env={"LC_ALL": "C"})


def test_qemu_ssh_uri_round_trip_is_secret_safe():
    config = QemuConfig(
        "102",
        transport="ssh",
        ssh=SshConfig(
            "hypervisor.example",
            username="root",
            password="secret",
        ),
    )

    uri = str(config)
    assert "secret" not in uri
    rebuilt = HostConfig(uri, password="secret")
    assert isinstance(rebuilt, QemuConfig)
    assert rebuilt.domain == "102"
    assert rebuilt.ssh.host == "hypervisor.example"
    assert str(rebuilt) == uri


def test_a_failed_probe_does_not_freeze_the_guest_family(monkeypatch):
    """`_commands` is the "already discovered" flag.

    Committing it before the optional osinfo/hostname probes meant one
    transient probe failure froze a Windows guest as POSIX for the object's
    lifetime, and a retried connect() silently succeeded without re-probing.
    """
    calls = []

    class _Transport:
        def __init__(self):
            self.fail = True

        def connect(self):
            pass

        def close(self):
            pass

        def execute(self, request, timeout=None):
            command = request.get("execute")
            calls.append(command)
            if command == "guest-info":
                return {
                    "supported_commands": [
                        {"name": "guest-get-osinfo", "enabled": True}
                    ]
                }
            if command == "guest-get-osinfo":
                if self.fail:
                    raise ConnectionError("agent busy")
                return {"id": "mswindows", "name": "Microsoft Windows"}
            return {}

    transport = _Transport()
    host = QemuHost(
        QemuConfig("vm", transport="libvirt", transport_factory=lambda: transport)
    )

    with pytest.raises(Exception):
        host.connect()

    # The failure must not have been cached as a successful discovery.
    transport.fail = False
    calls.clear()
    host.connect()

    assert "guest-get-osinfo" in calls


def test_degraded_provider_is_probed_once_not_once_per_path(caplog):
    """A loop over 500 guest files emitted 500 identical WARNING lines,
    because `path()` re-probed the provider on every call."""
    host, _ = _host()
    caplog.set_level("WARNING", logger="hostctl")

    for index in range(4):
        host.path(f"/tmp/file{index}")

    degraded = [
        record for record in caplog.records if "degraded" in record.getMessage()
    ]
    assert len(degraded) == 1


def test_a_supplied_path_helper_reaches_the_backend_and_lifts_the_degradation():
    """The helper surface existed on the backend and nothing could supply
    one, so every real QemuHost path was metadata-less."""
    import stat as stat_module

    from pathlib_next.utils.stat import FileStat

    class _Helper:
        def stat(self, path, *, follow_symlinks=True):
            return FileStat(st_mode=stat_module.S_IFREG | 0o644, st_size=3)

        def scandir(self, path):
            return []

    transport = _Transport()
    host = QemuHost(
        QemuConfig(
            "guest",
            transport="libvirt",
            transport_factory=lambda: transport,
            path_helper=_Helper(),
        )
    )

    path = host.path("/etc/motd")

    assert path.backend.helper is not None
    assert path.stat().st_size == 3
    assert host.path_provider.probe().availability == "available"
