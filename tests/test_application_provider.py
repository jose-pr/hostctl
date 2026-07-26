from hostctl import HostPath, OperationNotStarted, PosixHost
from hostctl.provider import PathProvider, ProviderProbe
import pytest
from examples.application_provider import (
    DownloadProvider,
    MetadataProvider,
    SftpProvider,
)


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
    assert path.via("sftp").provider.name == "sftp"


def test_application_provider_operation_selection_and_pinning(tmp_path):
    content = tmp_path / "payload"
    content.write_bytes(b"download")

    host = PosixHost(
        path_providers=(
            MetadataProvider(lambda *parts: HostPath(*parts)),
            DownloadProvider(lambda *parts: HostPath(*parts)),
            SftpProvider(lambda *parts: HostPath(*parts)),
        )
    )
    path = host.path(content)
    assert path.read_bytes() == b"download"
    pinned = path.via("sftp")
    assert pinned.provider.name == "sftp"
    with pytest.raises(NotImplementedError):
        path.via("download").write_bytes(b"mutation")
