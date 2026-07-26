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


def test_application_provider_operation_selection_and_pinning():
    metadata_backend = MemPathBackend()
    download_backend = MemPathBackend()
    sftp_backend = MemPathBackend()
    MemPath("payload", backend=download_backend).write_bytes(b"download")
    MemPath("payload", backend=sftp_backend).write_bytes(b"sftp")

    calls = []

    def sftp(*parts):
        calls.append("sftp")
        raise OperationNotStarted("SFTP preflight unavailable")

    def download(*parts):
        calls.append("download")
        return MemPath(*parts, backend=download_backend)

    host = PosixHost(
        path_providers=(
            MetadataProvider(lambda *parts: MemPath(*parts, backend=metadata_backend)),
            DownloadProvider(download),
            SftpProvider(sftp),
        )
    )
    path = host.path("payload")
    assert path.provider.name == "metadata"
    # Reorder only this example instance so SFTP is attempted before the
    # read-only download leg. The declared operation capabilities cause the
    # metadata provider to be skipped for content reads.
    host = PosixHost(
        path_providers=(
            MetadataProvider(lambda *parts: MemPath(*parts, backend=metadata_backend)),
            SftpProvider(sftp),
            DownloadProvider(download),
        )
    )
    assert host.path("payload").read_bytes() == b"download"
    assert calls == ["sftp", "download"]

    available = PosixHost(
        path_providers=(
            MetadataProvider(lambda *parts: MemPath(*parts, backend=metadata_backend)),
            SftpProvider(lambda *parts: MemPath(*parts, backend=sftp_backend)),
            DownloadProvider(download),
        )
    )
    pinned = available.path("payload").via("sftp")
    assert pinned.provider.name == "sftp"
    assert pinned.read_bytes() == b"sftp"
    with pytest.raises(NotImplementedError):
        available.path("payload").via("download").write_bytes(b"mutation")


def test_application_provider_does_not_replay_a_started_failure():
    calls = []

    def started(*parts):
        calls.append("sftp")
        raise RuntimeError("content operation may have started")

    def fallback(*parts):
        calls.append("download")
        return MemPath(*parts, backend=MemPathBackend())

    host = PosixHost(
        path_providers=(
            MetadataProvider(lambda *parts: MemPath(*parts, backend=MemPathBackend())),
            SftpProvider(started),
            DownloadProvider(fallback),
        )
    )

    with pytest.raises(RuntimeError, match="may have started"):
        host.path("payload").read_bytes()
    assert calls == ["sftp"]
