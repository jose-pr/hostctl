"""The SFTP backend of an SSH host: SFTP channels on the host's own connection."""

from __future__ import annotations

import typing

from ... import _async

if typing.TYPE_CHECKING:
    from ._transport import _SSHTransport

_sftppath = _async.sftppath()


class _SharedSFTPBackend(_sftppath.BaseSFTPBackend):
    """The one backend a host's SFTP paths share.

    Every request goes to the transport's `AsyncSSHBackend.over_connection()`
    backend for the connection `run()` uses, which is bound to one connection
    object: resolving it per request is what lets a path built before
    `close()` or a reconnect run on the next connection, and lets building a
    path connect nothing.
    """

    __slots__ = ("_transport",)

    supports_lchmod = _sftppath.AsyncSSHBackend.supports_lchmod
    supports_hardlink = _sftppath.AsyncSSHBackend.supports_hardlink
    supports_tree = _sftppath.AsyncSSHBackend.supports_tree

    def __init__(self, transport: _SSHTransport) -> None:
        self._transport = transport

    @property
    def host(self) -> str:
        return self._transport.config.host

    @property
    def port(self) -> typing.Optional[int]:
        # `None`: the ssh_config's `Port`, else 22, as `SFTPEndpoint` reads it.
        return self._transport.config.port

    @property
    def user(self) -> typing.Optional[str]:
        return self._transport.config.username

    @property
    def endpoint(self) -> typing.Any:
        return _sftppath.SFTPEndpoint(self.host.lower(), self.port, self.user)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.host!r}, {self.port})"

    def __reduce__(self) -> typing.NoReturn:
        raise TypeError(
            "an SSH host's SFTP backend cannot be pickled: its connection "
            "belongs to this process"
        )

    def _current(self) -> typing.Any:
        return self._transport._sftp_over_connection()

    def client(self) -> typing.Any:
        return self._current().client()

    def close(self) -> None:
        """Close the SFTP channel; the connection is the host's to close."""
        self._transport._close_sftp_channel()

    def _wire_open_mode(self, mode: str) -> str:
        return self._current()._wire_open_mode(mode)

    def tree_copy(self, path, target, **options) -> None:
        return self._current().tree_copy(path, target, **options)

    def tree_rm(self, path, **options) -> None:
        return self._current().tree_rm(path, **options)

    def checksum(self, path, algorithm: str) -> str:
        return self._current().checksum(path, algorithm)

    def supported_checksums(self, path) -> typing.FrozenSet[str]:
        return self._current().supported_checksums(path)
