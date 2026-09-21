"""What the QEMU host will and will not infer about a guest."""

from __future__ import annotations

import io
import zipfile

import pytest

from hostctl.host import HostConfig
from hostctl.host.qemu import QemuConfig, QemuHost

_FILE_COMMANDS = (
    "guest-file-open",
    "guest-file-read",
    "guest-file-write",
    "guest-file-close",
)


class _Transport:
    """A guest agent whose advertised command set and osinfo are given."""

    def __init__(self, *, commands=(), osinfo=None, content=b""):
        self.commands = tuple(commands)
        self.osinfo = osinfo
        self.content = content
        self.position = 0
        self.requests = []

    def execute(self, request, timeout=None):
        command = request["execute"]
        arguments = request.get("arguments", {})
        self.requests.append(command)
        if command == "guest-ping":
            return {}
        if command == "guest-info":
            return {
                "supported_commands": [
                    {"name": name, "enabled": True}
                    for name in _FILE_COMMANDS + self.commands
                ]
            }
        if command == "guest-get-osinfo":
            return self.osinfo or {}
        if command == "guest-file-open":
            return 3
        if command == "guest-file-close":
            return {}
        if command == "guest-file-seek":
            whence = arguments["whence"]
            name = whence["name"] if isinstance(whence, dict) else whence
            base = {
                "set": 0,
                "cur": self.position,
                "end": len(self.content),
                0: 0,
                1: self.position,
                2: len(self.content),
            }[name]
            self.position = max(0, base + arguments["offset"])
            return {
                "position": self.position,
                "eof": self.position >= len(self.content),
            }
        if command == "guest-file-read":
            import base64

            chunk = self.content[self.position : self.position + arguments["count"]]
            self.position += len(chunk)
            return {
                "count": len(chunk),
                "buf-b64": base64.b64encode(chunk).decode("ascii"),
                "eof": self.position >= len(self.content),
            }
        raise AssertionError(command)

    def close(self):
        pass


def _host(**kwargs):
    transport = _Transport(**kwargs)
    return (
        QemuHost(QemuConfig("guest", transport_factory=lambda: transport)),
        transport,
    )


def test_an_agent_that_names_no_family_refuses_to_guess():
    """A negative result from a probe that never ran is a guess.

    `guest-get-osinfo` can be blocklisted -- that is what `enabled` in
    `guest-info` is for -- and then "not Windows" was read out of an empty
    dict: a hardened Windows guest silently became POSIX sh, and
    `path(r"C:\\Temp")` came back a POSIX path.
    """
    host, _ = _host()

    with pytest.raises(NotImplementedError, match="guest-get-osinfo"):
        host.shell_flavour

    with pytest.raises(NotImplementedError, match="dialect"):
        host.path("/tmp/x")


def test_a_family_exclusive_command_is_positive_evidence():
    """`guest-get-devices` exists only on Windows agents."""
    host, _ = _host(commands=("guest-get-devices",))
    assert host.shell_flavour.name == "powershell"

    host, _ = _host(commands=("guest-get-cpustats",))
    assert host.shell_flavour.name == "posix"


def test_an_explicit_selection_needs_no_probe():
    """The settings the error names really do resolve it."""
    transport = _Transport()
    config = QemuConfig(
        "guest",
        transport_factory=lambda: transport,
        path_flavor="posix",
        dialect="posix",
    )
    host = QemuHost(config)

    assert host.shell_flavour.name == "posix"
    assert str(host.path("/tmp/x")) == "/tmp/x"


def test_os_family_is_a_family_not_a_distribution():
    """The same machine answered `linux` over SSH and `ubuntu` over QGA."""
    host, _ = _host(
        commands=("guest-get-osinfo",),
        osinfo={
            "id": "ubuntu",
            "kernel-name": "Linux",
            "pretty-name": "Ubuntu 24.04.1 LTS",
        },
    )

    info = host.info()
    assert info.os_family == "linux"
    assert info.os_name == "Ubuntu 24.04.1 LTS"


def test_a_guest_reader_is_seekable_where_the_agent_supports_it():
    """`guest-file-seek` was implemented, documented, and unreachable.

    `open("rb")` answered `seekable() == False`, so `zipfile` -- which
    looks at the end of the file for the central directory -- raised
    `io.UnsupportedOperation` against a guest path.
    """
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("hello.txt", "hi")
    host, _ = _host(
        commands=("guest-file-seek", "guest-get-osinfo"),
        osinfo={"id": "ubuntu", "kernel-name": "Linux"},
        content=payload.getvalue(),
    )

    with host.path("/srv/archive.zip").open("rb") as stream:
        assert stream.seekable()
        with zipfile.ZipFile(stream) as archive:
            assert archive.read("hello.txt") == b"hi"


def test_a_forward_only_reader_says_so():
    host, _ = _host(
        commands=("guest-get-osinfo",),
        osinfo={"id": "ubuntu", "kernel-name": "Linux"},
        content=b"0123456789",
    )

    with host.path("/srv/blob").open("rb") as stream:
        assert not stream.seekable()
        assert stream.read(4) == b"0123"


def test_an_exclusive_create_is_refused_by_open():
    """The refusal used to arrive from `close()`, after all the work.

    `staged_open` returned a writable stream and only called `write_bytes`
    at close time, so the caller streamed the whole payload into memory and
    then got a `NotImplementedError` out of the context manager's exit --
    where code that guards `open()` with a capability check is not looking.
    """
    host, _ = _host(
        commands=("guest-get-osinfo",),
        osinfo={"id": "ubuntu", "kernel-name": "Linux"},
    )

    with pytest.raises(NotImplementedError, match="exclusive"):
        host.path("/run/deploy.lock").open("xb")


def test_ssh_credentials_are_refused_on_a_socket_uri():
    """Accepted and discarded, a password read as authentication applied."""
    with pytest.raises(ValueError, match="password"):
        HostConfig("qga+unix:///run/qga.sock?domain=guest", password="hunter2")

    with pytest.raises(ValueError, match="known_hosts"):
        HostConfig("qemu+libvirt:///guest", known_hosts=())
