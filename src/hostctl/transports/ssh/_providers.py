"""SSH executor and SFTP path providers."""

from __future__ import annotations


import typing


from ...exceptions import OperationNotStartedError

from ...provider._contracts import ExecutorProvider, PathProvider, ProviderProbe
from ...provider._selector import ProviderSelector


from ._config import SSHConfig
from ._transport import _SSHTransport, log


class SSHExecutorProvider(ExecutorProvider):
    """Lifecycle-owning SSH command provider used by :class:`PosixHost`."""

    def __init__(self, transport: _SSHTransport):
        self.transport = transport
        # SSH receives one finalized command string; argv arguments are
        # rendered by the host shell before dispatch.
        #
        # `spawn`/`tty` are declared because this provider implements
        # `spawn()`: the host selects on the capability, and a provider that
        # can open a session but does not say so is skipped for it.
        capabilities = set(transport.executor.executor_capabilities)
        capabilities |= {"spawn", "tty"} & transport.capabilities
        super().__init__("ssh", transport.executor, capabilities=capabilities)

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
                "SSH provider declining before dispatch: %s: %s",
                type(exc).__name__,
                ProviderSelector.redact(exc),
            )
            raise OperationNotStartedError(
                "SSH connection failed before dispatch", cause=exc
            ) from exc

    def close(self):
        self.transport.close()

    def info(self):
        return self.transport.info()

    @property
    def shell_executable(self):
        resolved = self.transport._resolved_dialect
        return resolved[2] if resolved is not None else None

    @property
    def login_shell(self):
        """What parses a delivered command line before its own shell does.

        The host reads this when it renders a `CommandLine`, because only
        the transport knows there is an extra parse at all.
        """
        return self.transport.login_shell

    def spawn(self, *args, **options):
        return self.transport.spawn(*args, **options)


class SFTPPathProvider(PathProvider):
    """SFTP path provider sharing the SSH transport lifecycle."""

    def __init__(self, transport: _SSHTransport):
        self.transport = transport
        super().__init__(
            "sftp", lambda *segments: transport.path(*segments), capabilities=("path",)
        )

    def probe(self):
        from ... import _async

        try:
            _async.sftppath()
        except ImportError:
            return ProviderProbe(
                "unavailable",
                reason=(
                    "the 'ssh' extra is incomplete: sftppath is not installed; "
                    "pip install hostctl[ssh]"
                ),
            )
        return ProviderProbe("available", capabilities=self.capabilities)

    # Closing is owned by SSHExecutorProvider for the shared transport.
    #
    # Composite paths call this before EVERY dispatch, so it stays cheap: the
    # client is cached after the first call. Opening it here keeps a failed
    # connection a pre-dispatch decline rather than an error mid-operation.
    def connect(self):
        from ... import _async

        try:
            client = getattr(self.transport._backend(), "client", None)
            if client is not None:
                client()
        except OperationNotStartedError:
            raise
        except Exception as exc:
            # sftppath translates asyncssh's errors but not its timeouts: on
            # 3.9 a connect timeout is `asyncio.TimeoutError`, not the builtin.
            mapped = _async.normalize_asyncssh_error(exc)
            if not isinstance(mapped, (OSError, TimeoutError)):
                raise
            log.debug(
                "SFTP provider declining before dispatch: %s: %s",
                type(mapped).__name__,
                ProviderSelector.redact(mapped),
            )
            raise OperationNotStartedError(
                "SFTP connection failed before dispatch", cause=mapped
            ) from exc


def ssh_providers(
    config: SSHConfig,
) -> typing.Tuple[SSHExecutorProvider, SFTPPathProvider]:
    """Build an executor and path provider sharing one SSH transport.

    This is the supported way to compose SSH into a host you assemble
    yourself, rather than taking the finished `PosixHost` that
    `SSHConfig._create_host()` returns:

        executors, paths = [], []
        run_provider, path_provider = ssh_providers(config.ssh)
        executors.append(run_provider)
        paths.append(path_provider)

    Both providers **must** share one transport: that is what makes them share
    a connection and a lifecycle, and `SSHExecutorProvider` is the one that
    owns close. Assembling them by hand from two transports type-checks and
    silently opens two connections, only one of which is ever closed -- which
    is precisely why this returns a pair instead of exposing the transport.
    """
    transport = _SSHTransport(config)
    return SSHExecutorProvider(transport), SFTPPathProvider(transport)
