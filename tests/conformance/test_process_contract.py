"""Provider-independent Process behavior.

Two distinct things are pinned here, and they are deliberately separated:

* the :class:`Process` *protocol* -- structural expectations any adapter must
  satisfy.  These use one in-memory stub and are NOT parametrized over
  providers: the stub is the subject, so fanning it out across six transports
  would report twelve passes for one object and imply cross-transport
  coverage that does not exist.
* provider ``spawn`` -- exercised for real against every fake that advertises
  the capability, plus a registry-wide audit that no provider can advertise
  ``spawn`` without implementing it.
"""

from __future__ import annotations

import io
import pytest

from hostctl.process import Process
from .providers import fake_providers, provider_context


class _MemoryProcess:
    def __init__(self):
        self._stdout = io.BytesIO("héllo".encode())
        self._stderr = io.BytesIO(b"error")
        self._written = bytearray()
        self._returncode = None
        self.closed = False

    @property
    def returncode(self):
        return self._returncode

    def write(self, data):
        self._written.extend(data.encode() if isinstance(data, str) else data)

    def read(self, size=-1):
        return self._stdout.read(size)

    def read_stderr(self, size=-1):
        return self._stderr.read(size)

    def send_eof(self):
        return None

    def resize(self, columns, rows, pixel_width=0, pixel_height=0):
        return None

    def wait(self, timeout=None):
        self._returncode = 0
        return 0

    def terminate(self):
        self._returncode = -15

    def kill(self):
        self._returncode = -9

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def test_process_protocol_runtime_shape_and_lifecycle():
    process = _MemoryProcess()
    assert isinstance(process, Process)
    assert process.read(1) == b"h"
    assert process.read() == "éllo".encode()
    assert process.read() == b""
    assert process.read_stderr(2) == b"er"
    process.write("ok")
    assert process._written == b"ok"
    assert process.wait() == 0
    assert process.wait() == 0
    process.close()
    process.close()
    assert process.closed


def test_process_eof_and_context_are_idempotent():
    process = _MemoryProcess()
    with process as current:
        assert current is process
        process.send_eof()
        assert process.wait() == 0
    process.close()
    assert process.closed


def _spawn_providers():
    return [item for item in fake_providers() if "spawn" in item.capabilities]


def _spawn(host, provider):
    """Start a process the way this transport allows.

    A serial console is an exclusive merged byte stream with no command
    channel, so it spawns a bare session; every other transport takes a
    startup command.  This is a real contract difference, not a fake
    limitation -- `SerialHost.spawn` rejects startup commands outright.
    """
    if "session" in provider.capabilities:
        return host.spawn()
    return host.spawn("echo process")


def test_registry_advertises_spawn_for_at_least_one_transport():
    """Guard the guard: a registry that advertises no spawn silently voids
    every spawn assertion below by leaving them unparametrized."""
    assert _spawn_providers(), "no fake provider exercises spawn"


@pytest.mark.parametrize("provider", _spawn_providers(), ids=lambda p: p.name)
def test_provider_spawn_runs_a_real_process(provider):
    """A provider advertising ``spawn`` must actually start and reap one.

    This drives the production Process adapter for each transport --
    ``SshProcess``, ``ContainerProcess``, ``SerialConsoleProcess`` -- over a
    real bidirectional channel, so an adapter that cannot start, read, or
    reap a process fails here rather than being skipped.
    """
    with provider_context(provider) as host:
        process = _spawn(host, provider)
        try:
            assert isinstance(process, Process)
            if "session" in provider.capabilities:
                # A console session has no child to exit; it is live until
                # closed, which is exactly what the contract promises.
                assert process.returncode is None
            else:
                assert process.wait(timeout=10) == 0
                assert process.returncode == 0
        finally:
            process.close()
        # close() is idempotent for every adapter.
        process.close()


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_spawn_capability_matches_the_host_implementation(provider):
    """Advertising ``spawn`` and implementing it must not diverge.

    This is the regression for the WinRM defect: its provider defined a
    ``spawn`` pass-through to a transport that had none, so ``SystemHost``'s
    ``getattr(provider, "spawn", None)`` guard passed and the call failed one
    frame deeper with ``AttributeError`` instead of the documented
    ``NotImplementedError``.  Both directions are checked -- an advertised
    capability must work, and a withheld one must fail cleanly.
    """
    advertised = "spawn" in provider.capabilities
    with provider_context(provider) as host:
        if advertised:
            process = _spawn(host, provider)
            try:
                assert isinstance(process, Process)
            finally:
                process.close()
            return

        # Not advertised: the host must decline in the documented way rather
        # than raising AttributeError from somewhere deeper in the transport.
        with pytest.raises(NotImplementedError):
            host.spawn("echo process")
