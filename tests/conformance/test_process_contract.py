"""Provider-independent Process behavior."""

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


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_process_protocol_runtime_shape_and_lifecycle(provider):
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


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_process_eof_and_context_are_idempotent(provider):
    process = _MemoryProcess()
    with process as current:
        assert current is process
        process.send_eof()
        assert process.wait() == 0
    process.close()
    assert process.closed


@pytest.mark.parametrize("provider", fake_providers(), ids=lambda p: p.name)
def test_provider_process_uses_transport_spawn_when_available(provider):
    if "spawn" not in provider.capabilities:
        pytest.skip(f"{provider.name} has no spawn capability")
    with provider_context(provider) as host:
        process = host.spawn("echo process")
        try:
            assert process.wait(timeout=5) == 0
        finally:
            process.close()
