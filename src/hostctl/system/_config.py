"""`SystemConfig`: logical system URIs and their provider options."""

from __future__ import annotations


import typing
from urllib.parse import unquote, quote, urlencode


from ..host._config import HostConfig
from ..host._timeout import (
    DEFAULT,
    Timeout,
    _URI_TIMEOUT_KEYS,
    _TimeoutField,
    _timeout_from_query,
    _timeout_query,
)
from ..uri._parse import _refuse_uri, reject_control_characters, strict_uri_query

if typing.TYPE_CHECKING:
    from ._host import SystemHost


class SystemConfig(HostConfig):
    """Base configuration for a logical system with one or more providers.

    Abstract: concrete system families are :class:`PosixConfig`,
    :class:`WindowsConfig`, and :class:`IOSConfig`.  Each binds ``host_type``
    and ``uri_scheme``; this class binds neither and cannot be instantiated
    into a host on its own.
    """

    #: Class-level URI scheme that *builds* :attr:`connection_uri`.  This is
    #: deliberately not named ``scheme``: ``HostConfig.scheme`` is a property
    #: derived by parsing the built URI, and a plain string would shadow it.
    #: The two flow one way -- ``uri_scheme`` builds the URI, ``scheme``
    #: reads it back -- so they cannot disagree.
    uri_scheme: typing.ClassVar[str] = ""

    #: Concrete `SystemHost` subclass this configuration creates.  Bound by
    #: each concrete config; ``None`` marks this base class as abstract.
    host_type: typing.ClassVar[typing.Optional[typing.Type["SystemHost"]]] = None

    #: Only `total`; at `DEFAULT` the selected provider applies its own (a
    #: WinRM provider its `WinRMConfig`'s). Connecting belongs to the
    #: transport configs in `provider_options`.
    _timeout_defaults = {"total": DEFAULT}

    #: `total` is the default of `run(timeout=)`.
    timeout = _TimeoutField()

    def __init__(
        self,
        authority: str = "localhost",
        *,
        shell: object = None,
        executor: typing.Optional[typing.Iterable[str]] = None,
        path: typing.Optional[typing.Iterable[str]] = None,
        provider_options: typing.Optional[typing.Mapping[str, object]] = None,
        initializer: typing.Optional[typing.Callable[..., object]] = None,
        timeout: typing.Union[float, None, Timeout] = Timeout(),
    ):
        super().__init__()
        self.timeout = timeout  # type: ignore[assignment]
        _refuse_uri(type(self).__name__, authority)
        for name, value in (("executor", executor), ("path", path)):
            if isinstance(value, (str, bytes)):
                raise TypeError(
                    f"{name}= takes a sequence of descriptor names, "
                    "e.g. ('local',), not a string"
                )
        self.authority = authority or "localhost"
        self.shell = shell
        #: Descriptor names in precedence order; `None` takes the host
        #: class's `default_providers()`, `()` composes none.
        self.executors = None if executor is None else tuple(executor)
        self.paths = None if path is None else tuple(path)
        self.options = {
            key: value
            for key, value in (
                ("provider_options", provider_options),
                ("initializer", initializer),
            )
            if value is not None
        }

    def __repr__(self) -> str:
        # `provider_options` and `initializer` stay out, as the dataclass
        # configs keep their credential and injection fields out: provider
        # options carry a transport's password.
        return (
            f"{type(self).__name__}(authority={self.authority!r}, "
            f"shell={self.shell!r}"
            + ("" if self.executors is None else f", executor={self.executors!r}")
            + ("" if self.paths is None else f", path={self.paths!r}")
            + ("" if self.timeout == Timeout() else f", timeout={self.timeout!r}")
            + ")"
        )

    @property
    def connection_uri(self) -> str:
        if not self.uri_scheme:
            # The abstract base registers no URI scheme, so it has no
            # round-trippable URI to advertise, and a URI that cannot be
            # parsed back is worse than declining to produce one.
            raise NotImplementedError(
                f"{type(self).__name__} is abstract and has no connection URI"
            )
        query: typing.List[typing.Tuple[str, str]] = [
            ("executor", value) for value in self.executors or ()
        ]
        query += [("path", value) for value in self.paths or ()]
        if self.shell is not None:
            query.append(("shell", getattr(self.shell, "name", str(self.shell))))
        query += _timeout_query(self.timeout)
        return f"{self.uri_scheme}://{quote(self.authority, safe='')}" + (
            f"?{urlencode(query)}" if query else ""
        )

    @classmethod
    def _from_parsed_uri(cls, parsed, **credentials):
        constructor_only = {}
        for key in ("provider_options", "initializer"):
            if key in credentials:
                constructor_only[key] = credentials.pop(key)
        if credentials:
            names = ", ".join(sorted(str(key) for key in credentials))
            raise ValueError(
                "system URI reconstruction accepts only provider_options= and "
                f"initializer= constructor options; unsupported credentials: {names}"
            )
        # The SHARED parser, with the two keys that legitimately repeat
        # declared.
        query = strict_uri_query(
            parsed,
            ("executor", "path", "shell", *_URI_TIMEOUT_KEYS),
            repeatable=("executor", "path"),
        )
        # An absent key is "not named", so the host's defaults apply.
        executors = typing.cast(
            typing.Optional[typing.Tuple[str, ...]], query.get("executor")
        )
        paths = typing.cast(typing.Optional[typing.Tuple[str, ...]], query.get("path"))
        # The whole netloc, userinfo included, is unquoted below, so a
        # percent-encoded control character there would arrive decoded.
        reject_control_characters(parsed.netloc)
        return cls(
            # Unquoted: `connection_uri` percent-encodes the authority, so
            # storing the still-encoded netloc added a layer on every
            # `HostConfig(str(config))` round trip -- `node:22` became
            # `node%3A22`, then `node%253A22` -- and the API header promises
            # `str(config)` can be handed straight back.
            unquote(parsed.netloc) or unquote(parsed.path) or "localhost",
            shell=query.get("shell"),
            executor=executors,
            path=paths,
            timeout=_timeout_from_query(query),
            **constructor_only,
        )

    def _create_host(self):
        if self.host_type is None:
            from ._kinds import IOSConfig, PosixConfig, WindowsConfig

            names = ", ".join(
                item.__name__ for item in (PosixConfig, WindowsConfig, IOSConfig)
            )
            raise TypeError(
                f"{type(self).__name__} is abstract and creates no host; "
                f"use a concrete system configuration ({names})"
            )
        # The host resolves the descriptors itself: resolvers receive it.
        return self.host_type(self)
