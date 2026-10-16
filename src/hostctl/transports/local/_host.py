"""Local host implementation."""

from __future__ import annotations

import os as _os
import platform as _platform
import typing as _ty

from ...exceptions import OperationNotStartedError
from ._providers import LocalExecutorProvider, LocalPathProvider
from ...system._host import SystemHost
from ...host._config import HostConfig
from ...host._info import HostInfo, normalize_os_family
from pathapi import Path
from ...executor import PathLike
from ...shell import POSIX_SHELL, POWERSHELL
from ...shell._flavour import ShellFlavour


def local_providers() -> _ty.Tuple[LocalExecutorProvider, LocalPathProvider]:
    """This machine's executor and path providers, for a host you assemble yourself.

    A pair, like `ssh_providers()`, but with no transport to share.
    """
    return LocalExecutorProvider(), LocalPathProvider()


class LocalConfig(HostConfig, schemes=("local",)):
    """This machine, addressed as `local:`.

    It takes no host, no port and no credentials -- there is nothing to
    connect to -- so it is the one config whose URI is a bare scheme.
    """

    #: Declared rather than enforced inside `_from_parsed_uri`, so the
    #: whitelist can be read before a config is built: an ambient
    #: credential (the CLI's `HOSTCTL_PASSWORD`) is offered only where it
    #: is accepted. The base dispatcher enforces it.
    uri_credentials = ()

    def __init__(self) -> None:
        super().__init__()

    @property
    def connection_uri(self) -> str:
        return "local:"

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> LocalConfig:
        if parsed.netloc or parsed.path or parsed.query:
            # `local://` is the same URI with an empty authority, so the
            # message names both spellings.
            raise ValueError("local URI must be 'local:' or 'local://'")
        return cls()

    def _create_host(self) -> LocalHost:
        return LocalHost(self)


class LocalHost(SystemHost):
    """This machine, assembled from the local providers.

    A `SystemHost` like every other, not a second implementation of one:
    provider selection, failover, capabilities and the direct/shell
    dispatch are `SystemHost`'s, so local and remote hosts cannot diverge.

    What is genuinely local stays here: the system family and shell come
    from `os.name` rather than a config, `info()` reports this process's
    own platform, and the default providers are the local ones.
    """

    config_type = LocalConfig

    def __init__(
        self,
        config: _ty.Optional[LocalConfig] = None,
        *,
        executor_providers: _ty.Iterable[object] = (),
        path_providers: _ty.Iterable[object] = (),
        **options: object,
    ) -> None:
        # Instance attributes, set before `super().__init__` reads them: the
        # family is a property of the machine this process is on, not of a
        # subclass someone declared.
        # Resolved here, but NOT enforced: an exotic `os.name` could always
        # build a host and use `info()` and `path()`; only naming its shell
        # raised. `shell_flavour` below keeps that boundary.
        if _os.name == "nt":
            self.system_family = "windows"
            self.default_shell = POWERSHELL
        elif _os.name == "posix":
            self.system_family = "posix"
            self.default_shell = POSIX_SHELL
        else:
            self.system_family = "generic"
            self.default_shell = None
        super().__init__(
            config if config is not None else LocalConfig(),
            executor_providers=tuple(executor_providers) or (LocalExecutorProvider(),),
            path_providers=tuple(path_providers) or (LocalPathProvider(),),
            **options,
        )

    @property
    def shell_flavour(self) -> ShellFlavour:
        """The local shell, or a refusal naming the OS it could not place."""
        if self._shell is None and self._shell_resolver is None:
            raise NotImplementedError(f"unsupported local OS: {_os.name}")
        return super().shell_flavour

    def path(self, *segments: PathLike, backend: _ty.Optional[str] = None) -> Path:
        """A plain `pathapi` path, not a composite.

        The composite exists to select between providers per operation and
        to pin a route; a local host reaches one filesystem through one
        provider, so it would add a layer with nothing to decide. Callers
        also rely on a local path being an ordinary pathlib object.

        With no segments this returns the empty relative path, as every
        other transport does, not `os.getcwd()`; it still resolves against
        this process's working directory.
        """
        names = tuple(provider.name for provider in self._path_selector.providers)
        if backend is not None and backend not in names:
            raise ValueError(
                "local path backend must be "
                + " or ".join(repr(name) for name in names or ("local",))
            )
        if backend is None:
            provider = self._path_selector.select().provider
        else:
            provider = next(
                item for item in self._path_selector.providers if item.name == backend
            )
            if not provider.probe().usable:
                raise OperationNotStartedError(
                    f"path provider {backend!r} is unavailable"
                )
        return provider.path(*segments)

    def info(self) -> HostInfo:
        """This process's own platform, with no transport to ask."""
        return HostInfo(
            hostname=_platform.node() or None,
            os_family=normalize_os_family(_platform.system()),
            os_name=_platform.system() or None,
            os_version=_platform.version() or None,
            architecture=_platform.machine() or None,
        )
