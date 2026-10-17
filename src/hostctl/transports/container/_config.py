"""Container configuration: which container, engine and shell."""

from __future__ import annotations

import dataclasses
import os
import typing
from urllib.parse import quote, unquote, urlencode


from ...shell import shell_flavour
from ...host._config import HostConfig
from ...host._dispatch import _keyword_only
from ...host._selection import PathnameSelection, ShellSelection
from ...host._verify import (
    _TLSVerify,
    _tls_verify,
    _verify_from_query,
    _verify_query_text,
)
from ...host._timeout import (
    _URI_TIMEOUT_KEYS,
    _TimeoutField,
    _timeout_from_query,
    _timeout_query,
)
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

    #: docker-py's `DEFAULT_TIMEOUT_SECONDS`. No `total`: an Engine exec
    #: cannot be cancelled, and the buffered one reads a single response.
    _timeout_defaults = {"connect": 60.0}

    container: str
    engine_url: typing.Optional[str] = None
    #: The Engine's certificate check on an `https://` `engine_url`: `True` is
    #: the operating system's trust store (`REQUESTS_CA_BUNDLE` or
    #: `CURL_CA_BUNDLE` instead when set), `False` checks nothing, a path is
    #: the CA bundle trusted instead. Any other engine URL, the
    #: environment's Docker settings and a `client_factory` own their TLS, so
    #: there it must stay `True`.
    verify: typing.Union[bool, str, "os.PathLike[str]"] = True
    username: typing.Optional[str] = None
    workdir: typing.Optional[str] = None
    executable: typing.Optional[str] = None
    shell: ShellSelection = "auto"
    pathname: PathnameSelection = "auto"
    #: `connect` is the Docker SDK client's request timeout (60 s): it
    #: bounds connecting to the Engine and each API reply.
    timeout: _TimeoutField = _TimeoutField()
    client_factory: typing.Optional[typing.Callable[..., object]] = dataclasses.field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        HostConfig.__init__(self)
        if not self.container:
            raise ValueError("container must not be empty")
        _refuse_uri("ContainerConfig", self.container)
        self.verify = _tls_verify("ContainerConfig", self.verify)
        if self.verify is not True:
            if self.client_factory is not None:
                raise ValueError(
                    "ContainerConfig: client_factory builds the Docker client and "
                    "owns its TLS settings; leave verify=True"
                )
            if not (self.engine_url or "").casefold().startswith("https://"):
                raise ValueError(
                    "ContainerConfig: verify applies to an https:// engine_url; "
                    "other engine URLs carry no TLS, and without one docker-py "
                    "reads DOCKER_TLS_VERIFY and DOCKER_CERT_PATH"
                )
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
        if self.verify is not True:
            query["verify"] = _verify_query_text(typing.cast(_TLSVerify, self.verify))
        pairs = list(query.items()) + _timeout_query(self.timeout)
        return f"docker://{quote(self.container, safe='')}?{urlencode(pairs)}"

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials: object) -> ContainerConfig:
        query = strict_uri_query(
            parsed,
            {
                "engine_url",
                "verify",
                "username",
                "workdir",
                "executable",
                "shell",
                "pathname",
                *_URI_TIMEOUT_KEYS,
            },
        )
        if not parsed.netloc or parsed.path not in ("", "/"):
            raise ValueError("Docker URI requires a container and no path")
        if parsed.username or parsed.password or parsed.port:
            raise ValueError("Docker URI authority must contain only a container name")
        return cls(
            container=unquote(parsed.netloc),
            engine_url=query.get("engine_url") or None,
            verify=_verify_from_query(query, bundle=True),
            username=query.get("username") or None,
            workdir=query.get("workdir") or None,
            executable=query.get("executable") or None,
            shell=(
                "auto"
                if query.get("shell", "auto") == "auto"
                else shell_flavour(query["shell"])
            ),
            pathname=pathname_selection(query.get("pathname", "auto"), allow_auto=True),
            timeout=_timeout_from_query(query),
            client_factory=typing.cast(
                typing.Optional[typing.Callable[..., object]],
                credentials.get("client_factory"),
            ),
        )

    def _create_host(self) -> ContainerHost:
        from ._host import ContainerHost

        return ContainerHost(self)
