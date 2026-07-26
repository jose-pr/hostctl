from hostctl import HostPath, PosixHost
from hostctl.provider import PathProvider, ProviderProbe


def test_application_provider_prefers_sftp_after_rpc_probe_declines():
    rpc = PathProvider(
        "rpc",
        lambda *parts: HostPath(*parts),
        probe=lambda: ProviderProbe("unavailable", "metadata endpoint offline"),
    )
    sftp = PathProvider("sftp", lambda *parts: HostPath(*parts))
    host = PosixHost(path_providers=(rpc, sftp))
    path = host.path("etc", "hosts")
    assert path.provider.name == "sftp"
    assert str(path) == "etc\u005chosts" or str(path) == "etc/hosts"
    assert path.via("sftp") is path
