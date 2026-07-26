from hostctl import HostPath, OperationNotStarted, PosixHost
from hostctl.provider import PathProvider, ProviderProbe
import pytest
from pathlib_next.mempath import MemPath, MemPathBackend
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
    metadata_backend = MemPathBackend()
    download_backend = MemPathBackend()
    sftp_backend = MemPathBackend()

    def download(*parts):
        download_backend.setdefault("payload", bytearray(b"download"))
        return MemPath(*parts, backend=download_backend)

    calls = []

    def sftp(*parts):
        calls.append("sftp")
        if len(calls) == 1:
            raise OperationNotStarted("SFTP preflight unavailable")
        return MemPath(*parts, backend=sftp_backend)

    host = PosixHost(
        path_providers=(
            MetadataProvider(lambda *parts: MemPath(*parts, backend=metadata_backend)),
            DownloadProvider(download),
            SftpProvider(sftp),
        )
    )
    path = host.path("payload")
    assert path.provider.name == "metadata"
    with pytest.raises(OperationNotStarted):
        path.via("sftp")
    pinned = path.via("sftp")
    assert pinned.provider.name == "sftp"
    assert calls == ["sftp", "sftp"]
    with pytest.raises(NotImplementedError):
        path.via("download").write_bytes(b"mutation")
