"""The container exec and Docker archive providers."""

from __future__ import annotations

import logging


from ...exceptions import OperationNotStartedError
from ...provider._contracts import ExecutorProvider, PathProvider, ProviderProbe
from ...provider._selector import ProviderSelector

log = logging.getLogger(__name__)


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
        # A `SYMTYPE` tar member is a faithful archive representation, so
        # these ARE implemented -- and leaving them out of the declaration
        # made a composed host refuse operations its backend performs.
        "symlink_to",
        "readlink",
    )
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
        except OperationNotStartedError:
            raise
        # OSError, not just ConnectionError: a DNS failure (socket.gaierror)
        # and a routing failure (EHOSTUNREACH/ENETUNREACH) are OSError but not
        # ConnectionError, and they are unambiguously pre-dispatch -- no
        # channel opened, nothing sent -- so the provider declines and
        # ordered fallback moves on.
        except (OSError, TimeoutError) as exc:
            log.debug(
                "container provider declining before dispatch: %s: %s",
                type(exc).__name__,
                ProviderSelector.redact(exc),
            )
            raise OperationNotStartedError(
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
    fail with ``NotImplementedError`` at selection time *when this provider is
    composed into a `SystemHost`*; `ContainerHost` returns the backend's own
    path type, where the backend raises instead.  Either way the mutation is
    refused -- this is the plan's "read-only providers explicitly reject
    mutations instead of falling through" rule applied to a partially
    writable backend.
    """

    def __init__(self, factory, *, name="archive", probe=None):
        self._probe_hook = probe
        super().__init__(name, factory, capabilities=ARCHIVE_PATH_OPERATIONS)

    def probe(self) -> ProviderProbe:
        if self._probe_hook is None:
            return ProviderProbe("available", capabilities=self.capabilities)
        try:
            usable = self._probe_hook()
        except OperationNotStartedError:
            raise
        except (ConnectionError, TimeoutError, OSError) as exc:
            return ProviderProbe("unavailable", type(exc).__name__)
        if isinstance(usable, ProviderProbe):
            return usable
        return ProviderProbe(
            "available" if usable else "unavailable",
            "" if usable else "container archive API is unavailable",
            capabilities=self.capabilities,
        )
