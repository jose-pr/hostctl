"""`SystemConfig`: logical system URIs and their provider options."""

from __future__ import annotations


import typing
from urllib.parse import unquote, quote, urlencode


from ..host._config import HostConfig
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

    def __init__(
        self,
        authority: str = "localhost",
        *,
        shell: object = None,
        executor: typing.Iterable[str] = (),
        path: typing.Iterable[str] = (),
        provider_options: typing.Optional[typing.Mapping[str, object]] = None,
        initializer: typing.Optional[typing.Callable[..., object]] = None,
    ):
        super().__init__()
        _refuse_uri(type(self).__name__, authority)
        for name, value in (("executor", executor), ("path", path)):
            if isinstance(value, (str, bytes)):
                raise TypeError(
                    f"{name}= takes a sequence of descriptor names, "
                    "e.g. ('local',), not a string"
                )
        self.authority = authority or "localhost"
        self.shell = shell
        self.executors = tuple(executor)
        self.paths = tuple(path)
        self.options = {
            key: value
            for key, value in (
                ("provider_options", provider_options),
                ("initializer", initializer),
            )
            if value is not None
        }

    def _build_providers(self, kind: str):
        from ._resolvers import _SYSTEM_PROVIDER_RESOLVERS, _load_system_provider

        descriptors = self.executors if kind == "executor" else self.paths
        result = []
        for descriptor in descriptors:
            key = str(descriptor).casefold()
            resolver = _SYSTEM_PROVIDER_RESOLVERS.get(key) or _load_system_provider(key)
            if resolver is None:
                raise ValueError(f"unknown {kind} provider descriptor: {descriptor!r}")
            provider = resolver(self, kind)
            if provider is None:
                raise ValueError(
                    f"provider descriptor {descriptor!r} does not support {kind}"
                )
            result.append(provider)
        return tuple(result)

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
            ("executor", value) for value in self.executors
        ]
        query += [("path", value) for value in self.paths]
        if self.shell is not None:
            query.append(("shell", getattr(self.shell, "name", str(self.shell))))
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
            parsed, ("executor", "path", "shell"), repeatable=("executor", "path")
        )
        executors = typing.cast(typing.Tuple[str, ...], query.get("executor", ()))
        paths = typing.cast(typing.Tuple[str, ...], query.get("path", ()))
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
            **constructor_only,
        )

    def _create_host(self):
        from ._resolvers import _TRANSPORTS

        if self.host_type is None:
            from ._kinds import IOSConfig, PosixConfig, WindowsConfig

            names = ", ".join(
                item.__name__ for item in (PosixConfig, WindowsConfig, IOSConfig)
            )
            raise TypeError(
                f"{type(self).__name__} is abstract and creates no host; "
                f"use a concrete system configuration ({names})"
            )
        # A fresh transport cache per call: one host's executor and path
        # providers share a connection, and two hosts never do -- either one's
        # `close()` would tear down the connection the other is using.
        token = _TRANSPORTS.set({})
        try:
            executor_providers = self._build_providers("executor")
            path_providers = self._build_providers("path")
        finally:
            _TRANSPORTS.reset(token)
        return self.host_type(
            self,
            executor_providers=executor_providers,
            path_providers=path_providers,
        )
