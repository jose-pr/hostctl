"""QemuHost discovery and integration with an injected QGA transport."""

import base64

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
            return {
                "id": "mswindows" if self.windows else "linux",
                "pretty-name": "Windows" if self.windows else "Linux",
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
    assert arguments["env"] == ["NUMBER=7"]


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
