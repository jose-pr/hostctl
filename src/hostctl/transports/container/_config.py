"""Container configuration: which container, engine and shell."""

from __future__ import annotations

import dataclasses


import typing

from pathlib import PurePath

from urllib.parse import quote, unquote, urlencode

from pathapi import Pathname


from ...shell import ShellFlavourSelection, shell_flavour


from ...host._config import HostConfig


from ...uri._parse import (
    _refuse_uri,
    path_flavor_name,
    path_flavor_selection,
    strict_uri_query,
)

if typing.TYPE_CHECKING:
    from ._host import ContainerHost


PathnameConstructor = typing.Type[typing.Union[PurePath, Pathname]]

ContainerShellSelection = typing.Union[ShellFlavourSelection, typing.Literal["auto"]]

ContainerPathSelection = typing.Union[PathnameConstructor, typing.Literal["auto"]]


@dataclasses.dataclass
class ContainerConfig(HostConfig, schemes=("docker",)):
    """Docker Engine target and in-container execution defaults."""

    #: Declared rather than enforced inside `_from_parsed_uri`, so the
    #: whitelist can be read before a config is built: an ambient
    #: credential (the CLI's `HOSTCTL_PASSWORD`) is offered only where it
    #: is accepted. The base dispatcher enforces it.
    uri_credentials = ("client_factory",)

    container: str
    engine_url: typing.Optional[str] = None
    user: typing.Optional[str] = None
    workdir: typing.Optional[str] = None
    executable: typing.Optional[str] = None
    dialect: ContainerShellSelection = "auto"
    path_flavor: ContainerPathSelection = "auto"
    client_factory: typing.Optional[typing.Callable[..., object]] = dataclasses.field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.container:
            raise ValueError("container must not be empty")
        _refuse_uri("ContainerConfig", self.container)
        if self.dialect != "auto":
            self.dialect = shell_flavour(self.dialect)
        elif self.executable is not None:
            raise ValueError("executable cannot be combined with dialect='auto'")
        self.path_flavor = path_flavor_selection(self.path_flavor, allow_auto=True)

    @property
    def connection_uri(self) -> str:
        query: typing.Dict[str, object] = {
            "dialect": self.dialect,
            "path_flavor": path_flavor_name(self.path_flavor),
        }
        for name in ("engine_url", "user", "workdir", "executable"):
            value = getattr(self, name)
            if value is not None:
                query[name] = value
        return f"docker://{quote(self.container, safe='')}" + (
            f"?{urlencode(query)}" if query else ""
        )

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> ContainerConfig:
        query = strict_uri_query(
            parsed,
            {
                "engine_url",
                "user",
                "workdir",
                "executable",
                "dialect",
                "path_flavor",
            },
        )
        if not parsed.netloc or parsed.path not in ("", "/"):
            raise ValueError("Docker URI requires a container and no path")
        if parsed.username or parsed.password or parsed.port:
            raise ValueError("Docker URI authority must contain only a container name")
        return cls(
            container=unquote(parsed.netloc),
            engine_url=query.get("engine_url") or None,
            user=query.get("user") or None,
            workdir=query.get("workdir") or None,
            executable=query.get("executable") or None,
            dialect=(
                "auto"
                if query.get("dialect", "auto") == "auto"
                else shell_flavour(query["dialect"])
            ),
            path_flavor=path_flavor_selection(
                query.get("path_flavor", "auto"), allow_auto=True
            ),
            client_factory=typing.cast(
                typing.Optional[typing.Callable[..., object]],
                credentials.get("client_factory"),
            ),
        )

    def _create_host(self) -> ContainerHost:
        from ._host import ContainerHost

        return ContainerHost(self)
