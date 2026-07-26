"""Opt-in hardware/network serial smoke test; never runs by default."""

from __future__ import annotations

import os

import pytest

from hostctl import HostConfig, SerialConfig


@pytest.mark.skipif(
    not os.environ.get("HOSTCTL_TEST_SERIAL_URI"),
    reason="set HOSTCTL_TEST_SERIAL_URI to enable live serial smoke coverage",
)
def test_live_serial_uri_connects_and_closes():
    uri = os.environ["HOSTCTL_TEST_SERIAL_URI"]
    config = HostConfig(uri)
    assert isinstance(config, SerialConfig)
    with config as host:
        assert "session" in host.capabilities
