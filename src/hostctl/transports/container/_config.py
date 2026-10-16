"""Container configuration: which container, engine and shell."""

from __future__ import annotations

import dataclasses
import typing
from urllib.parse import quote, unquote, urlencode


from ...shell import shell_flavour
from ...host._config import HostConfig
from ...host._dispatch import _keyword_only
from ...host._selection import PathnameSelection, ShellSelection
from ...uri._parse import (
    _refuse_uri,
    pathname_label,
    pathname_selection,
    strict_uri_query,
)

if typing.TYPE_CHECKING:
    from ._host import ContainerHost


@_keyword_only()
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
    username: typing.Optional[str] = None
    workdir: typing.Optional[str] = None
    executable: typing.Optional[str] = None
    shell: ShellSelection = "auto"
    pathname: PathnameSelection = "auto"
    client_factory: typing.Optional[typing.Callable[..., object]] = dataclasses.field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.container:
            raise ValueError("container must not be empty")
        _refuse_uri("ContainerConfig", self.container)
        if self.shell != "auto":
            self.shell = shell_flavour(self.shell)
        elif self.executable is not None:
            raise ValueError("executable cannot be combined with shell='auto'")
        self.pathname = pathname_selection(self.pathname, allow_auto=True)

    @property
    def connection_uri(self) -> str:
        query: typing.Dict[str, object] = {
            "shell": self.shell,
            "pathname": pathname_label(self.pathname),
        }
        for name in ("engine_url", "username", "workdir", "executable"):
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
                "username",
                "workdir",
                "executable",
                "shell",
                "pathname",
            },
        )
        if not parsed.netloc or parsed.path not in ("", "/"):
            raise ValueError("Docker URI requires a container and no path")
        if parsed.username or parsed.password or parsed.port:
            raise ValueError("Docker URI authority must contain only a container name")
        return cls(
            container=unquote(parsed.netloc),
            engine_url=query.get("engine_url") or None,
            username=query.get("username") or None,
            workdir=query.get("workdir") or None,
            executable=query.get("executable") or None,
            shell=(
                "auto"
                if query.get("shell", "auto") == "auto"
                else shell_flavour(query["shell"])
            ),
            pathname=pathname_selection(query.get("pathname", "auto"), allow_auto=True),
            client_factory=typing.cast(
                typing.Optional[typing.Callable[..., object]],
                credentials.get("client_factory"),
            ),
        )

    def _create_host(self) -> ContainerHost:
        from ._host import ContainerHost

        return ContainerHost(self)
