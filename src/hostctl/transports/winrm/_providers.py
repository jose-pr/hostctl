"""WinRM executor and path providers."""

from __future__ import annotations


import typing


from ...exceptions import OperationNotStartedError
from ...provider._contracts import ExecutorProvider, PathProvider, ProviderProbe
from ...provider._selector import ProviderSelector
from ._config import WinRMConfig
from ._transport import _WinRMTransport, log


class WinRMExecutorProvider(ExecutorProvider):
    """Lifecycle-owning WinRM/PSRP executor provider."""

    def __init__(self, transport: _WinRMTransport):
        self.transport = transport
        # The executor already declares `manages_status` when its session
        # reports the status itself, which is what keeps the flavour's `exit`
        # epilogue off the script.
        capabilities = set(transport.executor.executor_capabilities)
        if "runspace" in transport.capabilities:
            capabilities.add("runspace")
        super().__init__("winrm", transport.executor, capabilities=capabilities)

    def probe(self):
        return ProviderProbe("available", capabilities=self.capabilities)

    def connect(self):
        try:
            self.transport.connect()
        except OperationNotStartedError:
            raise
        # OSError, not just ConnectionError: a DNS failure (socket.gaierror)
        # and a routing failure (EHOSTUNREACH/ENETUNREACH) are OSError but not
        # ConnectionError, and they are unambiguously pre-dispatch -- no
        # channel opened, nothing sent -- so the provider declines and
        # ordered fallback moves on.
        except (OSError, TimeoutError) as exc:
            log.debug(
                "WinRM provider declining before dispatch: %s: %s",
                type(exc).__name__,
                ProviderSelector.redact(exc),
            )
            from ...provider._decline import connect_failure

            config = self.transport.config
            raise OperationNotStartedError(
                connect_failure(
                    "WinRM",
                    config.host,
                    config.port,
                    exc,
                    trust=(
                        "name its CA bundle with verify= or pass verify=False "
                        "to skip the certificate check"
                    ),
                ),
                cause=exc,
            ) from exc

    def close(self):
        self.transport.close()

    def info(self):
        return self.transport.info()

    # No `spawn`: WinRM has no persistent byte-stream session (see
    # docs/guide/contracts.md). Defining a pass-through here would satisfy
    # SystemHost.spawn's `getattr(provider, "spawn", None)` guard and then
    # fail one frame deeper with AttributeError; omitting it lets that guard
    # raise the documented NotImplementedError instead.

    def runspace(self):
        if "runspace" not in self.capabilities:
            raise NotImplementedError("WinRM provider does not support PSRP runspaces")
        return self.transport.runspace()


class WinRMPathProvider(PathProvider):
    """Windows-semantic WinRM path provider sharing the executor transport."""

    def __init__(self, transport: _WinRMTransport):
        self.transport = transport
        super().__init__(
            "winrm", lambda *segments: transport.path(*segments), capabilities=("path",)
        )


def winrm_providers(
    config: WinRMConfig,
) -> typing.Tuple[WinRMExecutorProvider, WinRMPathProvider]:
    """Build an executor and path provider sharing one WinRM transport.

    The SSH counterpart, `hostctl.transports.ssh.ssh_providers`, documents why this
    returns a pair rather than exposing the transport: both providers must
    share one, and building them separately silently opens two.
    """
    transport = _WinRMTransport(config)
    return WinRMExecutorProvider(transport), WinRMPathProvider(transport)
