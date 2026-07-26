"""Application-specific path providers (pytruenas-style composition).

This deliberately contains no pytruenas imports.  An application can expose
metadata/RPC paths first and retain SFTP/download as ordered fallbacks.
"""

from __future__ import annotations

from hostctl import PathProvider, ProviderProbe


class ApplicationPathProvider(PathProvider):
    """Adapter for an application's path factory and optional health probe."""

    def __init__(self, name, factory, *, available=True, capabilities=("read",)):
        super().__init__(
            name,
            factory,
            capabilities=capabilities,
            probe=lambda: ProviderProbe(
                "available" if available else "unavailable",
                "application endpoint unavailable" if not available else "",
                frozenset(capabilities),
            ),
        )


class MetadataProvider(ApplicationPathProvider):
    """Metadata/RPC provider; mutations may decline before dispatch."""

    def __init__(self, factory, *, available=True):
        super().__init__(
            "metadata",
            factory,
            available=available,
            capabilities=("stat", "scandir", "read", "open_read", "write"),
        )


class DownloadProvider(ApplicationPathProvider):
    """Read-only content download provider."""

    def __init__(self, factory, *, available=True):
        super().__init__(
            "download",
            factory,
            available=available,
            capabilities=("stat", "read", "open_read"),
        )


class SftpProvider(ApplicationPathProvider):
    """Ordered SFTP fallback provider."""

    def __init__(self, factory, *, available=True):
        super().__init__(
            "sftp",
            factory,
            available=available,
            capabilities=("stat", "scandir", "read", "open_read", "write"),
        )


def providers(*, rpc, sftp, download=None):
    """Return an ordered RPC/SFTP/download provider collection.

    ``rpc`` is preferred for metadata and mutations; SFTP and download
    providers can be supplied as fallbacks by the caller.
    """
    result = [rpc, sftp]
    if download is not None:
        result.append(download)
    return tuple(result)
