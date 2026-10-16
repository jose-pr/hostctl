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


def _unreachable_host(port, **options):
    from hostctl import PosixHost, local_providers, ssh_providers
    from hostctl.transports.ssh import SSHConfig

    executor, sftp = ssh_providers(
        SSHConfig("127.0.0.1", port=port, username="root", verify=False, **options)
    )
    host = PosixHost(
        executor_providers=(executor,), path_providers=(sftp, local_providers()[1])
    )
    return host, executor.transport


def _free_port():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_relative_path_on_a_refused_port_is_built_by_the_next_provider():
    """The login directory cannot be read, and nothing was sent: the SFTP
    provider declines instead of raising from path construction."""
    host, _ = _unreachable_host(_free_port())
    try:
        assert host.path("etc/hosts").provider.name == "local"
        assert host.path().provider.name == "local"
    finally:
        host.close()


@pytest.mark.parametrize("pathname", (None, "auto"))
@pytest.mark.parametrize("absolute_first", (False, True))
def test_an_unresolvable_host_declines_whatever_ran_before(
    monkeypatch, pathname, absolute_first
):
    import socket

    from hostctl.exceptions import OperationNotStartedError

    options = {} if pathname is None else {"pathname": pathname}
    host, transport = _unreachable_host(_free_port(), **options)

    def unresolvable():
        raise socket.gaierror(11001, "getaddrinfo failed")

    monkeypatch.setattr(transport, "connect", unresolvable)
    try:
        if absolute_first:
            host.path("/etc/hosts")
        assert host.path("etc/hosts").provider.name == "local"
        with pytest.raises(OperationNotStartedError) as declined:
            transport.path("etc/hosts")
        assert isinstance(declined.value.__cause__, socket.gaierror)
    finally:
        host.close()
