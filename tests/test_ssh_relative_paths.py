"""A relative SSH path names the login directory, as `run()`'s working directory does."""

from __future__ import annotations

import pytest

pytest.importorskip("asyncssh")
pytest.importorskip("sftppath")

from conformance.inprocess_ssh import InProcessSSHServer  # noqa: E402


async def _handler(process):
    process.exit(0)


def _server(tmp_path):
    (tmp_path / "home" / "tester").mkdir(parents=True)
    return InProcessSSHServer(_handler, sftp_root=tmp_path, home="/home/tester")


def test_a_relative_path_lands_in_the_login_directory(tmp_path):
    with _server(tmp_path) as server:
        host = server.config()._create_host()
        try:
            host.path("rel.txt").write_text("x")
            host.path("/abs.txt").write_text("y")
            assert host.path().is_dir()
        finally:
            host.close()

    assert (tmp_path / "home" / "tester" / "rel.txt").read_text() == "x"
    assert (tmp_path / "abs.txt").read_text() == "y"


def test_the_login_directory_is_asked_once_per_connection(tmp_path):
    with _server(tmp_path) as server:
        host = server.config()._create_host()
        try:
            for name in ("a", "b", "c"):
                host.path(name).write_text(name)
            first = len(
                server.events and [e for e in server.events if e[0] == "realpath"]
            )
            host.close()
            host.path("d").write_text("d")
            second = len([e for e in server.events if e[0] == "realpath"])
        finally:
            host.close()

    assert first == 1
    assert second == 2
