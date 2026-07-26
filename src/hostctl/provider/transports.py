"""Path and executor provider adapters for the built-in transports.

The SSH/SFTP and WinRM adapters live beside their private transport services
(``host/_ssh.py``, ``host/_winrm.py``) because they own that transport's
lifecycle.  The adapters here wrap backends that are *not* lifecycle owners:
the local filesystem, the Docker archive API, and the QEMU Guest Agent file
RPCs.  Each one declares the operations its backend can genuinely perform, so
:class:`~hostctl.host.composite_path._CompositePathMixin` rejects an
unsupported mutation outright instead of falling through to another provider.

None of these adapters retries.  A backend failure is reported as-is; only a
proven pre-dispatch refusal is raised as
:class:`~hostctl.provider.OperationNotStarted`.
"""

from __future__ import annotations

import logging
import os
import typing

from pathlib_next import Path

from ._common import (
    ExecutorProvider,
    OperationNotStarted,
    PathProvider,
    ProviderProbe,
    ProviderSelector,
)

log = logging.getLogger("hostctl.provider.transports")

#: Operations a POSIX-complete filesystem backend performs.
FULL_PATH_OPERATIONS = frozenset(
    (
        "stat",
        "scandir",
        "open",
        "open_read",
        "open_write",
        "read",
        "write",
        "exists",
        "is_file",
        "is_dir",
        "mkdir",
        "chmod",
        "unlink",
        "rmdir",
        "rename",
    )
)

#: Content and metadata reads plus whole-file writes, with no namespace
#: mutations.  The Docker archive API can replace a file's bytes but cannot
#: create a directory, change a mode, or remove/rename an entry.
ARCHIVE_PATH_OPERATIONS = frozenset(
    (
        "stat",
        "scandir",
        "open",
        "open_read",
        "open_write",
        "read",
        "write",
        "exists",
        "is_file",
        "is_dir",
    )
)

#: QGA file RPCs without a guest helper: content only.  ``stat``/``scandir``
#: and every namespace mutation require a positively probed helper.
QGA_FILE_OPERATIONS = frozenset(("open", "open_read", "open_write", "read", "write"))

#: Operations a positively probed guest helper adds on top of the file RPCs.
QGA_HELPER_OPERATIONS = frozenset(
    (
        "stat",
        "scandir",
        "exists",
        "is_file",
        "is_dir",
        "mkdir",
        "chmod",
        "unlink",
        "rmdir",
        "rename",
    )
)


class LocalExecutorProvider(ExecutorProvider):
    """Local subprocess execution with native argv, cwd, and env support."""

    def __init__(self, executor=None):
        if executor is None:
            from ..executor import LocalExecutor

            executor = LocalExecutor()
        self.transport = executor
        super().__init__("local", executor)

    def probe(self) -> ProviderProbe:
        return ProviderProbe(
            "available", capabilities=self.capabilities, system_hint=os.name
        )


class LocalPathProvider(PathProvider):
    """Local filesystem paths; the process's own filesystem is always usable."""

    def __init__(self, factory: typing.Optional[typing.Callable[..., Path]] = None):
        super().__init__(
            "local",
            factory if factory is not None else (lambda *segments: Path(*segments)),
            capabilities=FULL_PATH_OPERATIONS,
        )

    def probe(self) -> ProviderProbe:
        return ProviderProbe(
            "available", capabilities=self.capabilities, system_hint=os.name
        )


class ContainerExecutorProvider(ExecutorProvider):
    """Docker ``exec`` command provider owning the container's inspection.

    ``connect`` inspects the container before any command is dispatched, so a
    missing or stopped container is a proven pre-dispatch refusal rather than a
    command whose outcome is unknown.
    """

    def __init__(self, executor, *, connect=None, close=None, info=None):
        self.transport = executor
        self._connect = connect
        self._close = close
        self._info = info
        super().__init__("container", executor)

    def probe(self) -> ProviderProbe:
        return ProviderProbe("available", capabilities=self.capabilities)

    def connect(self):
        if self._connect is None:
            return
        log.debug("inspecting container before dispatch")
        try:
            self._connect()
        except (ConnectionError, TimeoutError) as exc:
            log.debug(
                "container provider declining before dispatch: %s: %s",
                type(exc).__name__,
                ProviderSelector.redact(exc),
            )
            raise OperationNotStarted(
                "container is unavailable before dispatch", cause=exc
            ) from exc
        log.debug("container is running and ready for dispatch")

    def close(self):
        if self._close is not None:
            log.debug("releasing container provider resources")
            self._close()

    def info(self):
        if self._info is None:
            return None
        return self._info()


class ContainerArchivePathProvider(PathProvider):
    """Docker archive (``get_archive``/``put_archive``) path provider.

    The archive API has no namespace mutations.  Declaring only the operations
    it implements makes ``mkdir``/``chmod``/``unlink``/``rmdir``/``rename``
    fail with ``NotImplementedError`` at selection time, which is the plan's
    "read-only providers explicitly reject mutations instead of falling
    through" rule applied to a partially writable backend.
    """

    def __init__(self, factory, *, name="archive", probe=None):
        self._probe_hook = probe
        super().__init__(name, factory, capabilities=ARCHIVE_PATH_OPERATIONS)

    def probe(self) -> ProviderProbe:
        if self._probe_hook is None:
            return ProviderProbe("available", capabilities=self.capabilities)
        try:
            usable = self._probe_hook()
        except (ConnectionError, TimeoutError, OSError) as exc:
            return ProviderProbe("unavailable", type(exc).__name__)
        if isinstance(usable, ProviderProbe):
            return usable
        return ProviderProbe(
            "available" if usable else "unavailable",
            "" if usable else "container archive API is unavailable",
            capabilities=self.capabilities,
        )


class QgaPathProvider(PathProvider):
    """QEMU Guest Agent path provider gated on genuinely probed RPCs.

    QGA file RPCs give content access.  Metadata and namespace mutations exist
    only when a guest helper was positively probed, so this provider reports
    ``degraded`` when it can move bytes but cannot describe or restructure the
    guest filesystem.
    """

    def __init__(self, factory, backend, *, name="qga"):
        self.backend = backend
        capabilities = self._backend_capabilities(backend)
        super().__init__(name, factory, capabilities=capabilities)

    @staticmethod
    def _backend_capabilities(backend) -> frozenset:
        supported = frozenset(getattr(backend, "supported_commands", ()) or ())
        values = set()
        read_commands = getattr(
            type(backend),
            "_READ_COMMANDS",
            frozenset(("guest-file-open", "guest-file-read", "guest-file-close")),
        )
        write_commands = getattr(
            type(backend),
            "_WRITE_COMMANDS",
            frozenset(
                (
                    "guest-file-open",
                    "guest-file-write",
                    "guest-file-flush",
                    "guest-file-close",
                )
            ),
        )
        if read_commands <= supported:
            values.update(("read", "open", "open_read"))
        if write_commands <= supported:
            values.update(("write", "open_write"))
        if getattr(backend, "helper", None) is not None:
            values.update(QGA_HELPER_OPERATIONS)
        return frozenset(values)

    def probe(self) -> ProviderProbe:
        if not self.capabilities:
            return ProviderProbe(
                "unavailable", "guest agent provides no usable file RPCs"
            )
        if getattr(self.backend, "helper", None) is None:
            # Warning, not debug: the provider stays usable, so the operation
            # proceeds and nothing surfaces an error -- but the caller silently
            # loses stat/scandir and every namespace mutation.  Degrading
            # without saying so is exactly the case a log is for.
            log.warning(
                "QGA provider %s is degraded: no guest helper was probed, so "
                "metadata and namespace mutations are unavailable",
                ProviderSelector.redact(self.name),
            )
            return ProviderProbe(
                "degraded",
                "guest helper was not probed; metadata and mutations are unavailable",
                capabilities=self.capabilities,
            )
        return ProviderProbe("available", capabilities=self.capabilities)


class DownloadPathProvider(PathProvider):
    """Read-only content provider for an application download endpoint.

    Mutating operations are absent from the declared capability set, so the
    composite path rejects them rather than silently routing the mutation to
    another provider that might apply it to different state.
    """

    READ_OPERATIONS = frozenset(("stat", "read", "open", "open_read", "exists"))

    def __init__(self, factory, *, name="download", available=True, reason=""):
        self._available = bool(available)
        self._reason = reason
        super().__init__(name, factory, capabilities=self.READ_OPERATIONS)

    def probe(self) -> ProviderProbe:
        if not self._available:
            return ProviderProbe(
                "unavailable", self._reason or "download endpoint is unavailable"
            )
        return ProviderProbe("available", capabilities=self.capabilities)


__all__ = [
    "ARCHIVE_PATH_OPERATIONS",
    "ContainerArchivePathProvider",
    "ContainerExecutorProvider",
    "DownloadPathProvider",
    "FULL_PATH_OPERATIONS",
    "LocalExecutorProvider",
    "LocalPathProvider",
    "QGA_FILE_OPERATIONS",
    "QGA_HELPER_OPERATIONS",
    "QgaPathProvider",
]
