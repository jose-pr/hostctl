"""The QEMU Guest Agent path provider."""

from __future__ import annotations

import logging


import typing


from ...provider._contracts import PathProvider, ProviderProbe
from ...provider._selector import ProviderSelector

log = logging.getLogger("hostctl.transports.qemu")


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


class QGAPathProvider(PathProvider):
    """QEMU Guest Agent path provider gated on genuinely probed RPCs.

    QGA file RPCs give content access.  Metadata and namespace mutations exist
    only when a guest helper was positively probed, so this provider reports
    ``degraded`` when it can move bytes but cannot describe or restructure the
    guest filesystem.
    """

    def __init__(self, factory, backend, *, name="qga"):
        self.backend = backend
        capabilities = self._backend_capabilities(backend)
        self._probed: typing.Optional[typing.Tuple[bool, ProviderProbe]] = None
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
            # `exists` needs only the read RPCs: an open for reading tells
            # absent from present without a helper.
            values.update(("read", "open", "open_read", "exists"))
        if write_commands <= supported:
            values.update(("write", "open_write"))
        if getattr(backend, "helper", None) is not None:
            values.update(QGA_HELPER_OPERATIONS)
        return frozenset(values)

    def probe(self) -> ProviderProbe:
        # Memoized on the one thing the answer depends on. The verdict is a
        # property of the backend, not of the call, and recomputing it per
        # `QEMUHost.path()` meant a loop over 500 guest files emitted 500
        # identical degraded-provider warnings.
        helper_missing = getattr(self.backend, "helper", None) is None
        cached = self._probed
        if cached is not None and cached[0] == helper_missing:
            return cached[1]
        result = self._compute_probe(helper_missing)
        self._probed = (helper_missing, result)
        return result

    def _compute_probe(self, helper_missing: bool) -> ProviderProbe:
        if not self.capabilities:
            return ProviderProbe(
                "unavailable", "guest agent provides no usable file RPCs"
            )
        if helper_missing:
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
