"""A parsed connection target: what a user typed, turned into settings."""

from __future__ import annotations

import dataclasses as _dc
import typing as _ty
from urllib.parse import (
    SplitResult as _SplitResult,
    parse_qsl as _parse_qsl,
    quote as _quote,
    unquote as _unquote,
    urlsplit as _urlsplit,
)

from ._common import (
    _encode_stripped_characters,
    _reject_authority_control_characters,
    parse_credentials,
    uri_host,
    uri_hostname,
)

#: Loopback spellings recognised without resolving anything. Deliberately a
#: string comparison: `is_local` runs while a configuration is built, which is
#: documented as network-free, and a DNS lookup there would both block and
#: raise on an unresolvable name.
_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1", "[::1]"})


@_dc.dataclass(frozen=True)
class ConnectionString:
    """A connection target parsed from whatever a user typed.

    `ConnectionString("nas")`, `ConnectionString("nas:8443")`, and
    `ConnectionString("wss://root:pw@nas:8443/api")` all parse. A bare host is
    not an invalid URI -- it is a URI with the scheme left off, which is what
    people type on a command line.

    Every field can also be supplied directly, and three layers decide each
    one: **an explicit argument wins, then whatever the string carried, then
    `defaults`.**

    So `scheme=`, `port=`, and the rest are *overrides*, not defaults --
    `ConnectionString("wss://nas", scheme="ssh")` is `ssh`, the same way
    `port=` beats a port written in the string. Supplying a value only when
    the string omits one is exactly what `defaults` is for:

        ConnectionString("nas", scheme="wss")               # wss://nas
        ConnectionString("wss://nas", scheme="ssh")         # ssh://nas
        ConnectionString("wss://nas", defaults={"scheme": "ssh"})   # wss://nas
        ConnectionString("nas:9", scheme="wss", port=1)     # port=1 beats :9

    `defaults` accepts a mapping or another `ConnectionString`, so a partially
    filled one expresses "these settings unless the string says otherwise":

        profile = ConnectionString("wss://root@nas", port=443)
        ConnectionString("other", defaults=profile)      # wss://root@other:443

    Only fields a `ConnectionString` actually carries count as defaults, so an
    empty `path`/`query`/`fragment` never overrides. `default_ports` maps a
    scheme to its port and applies last, once the scheme is known -- `ws`/`wss`
    are the case in point, since no system services database knows them.

    The host keeps the spelling it was given. `urlsplit().hostname` is
    case-folded, which is right for *resolution* and wrong for text rendered
    back to a caller, so `nasA` stays `nasA` in `host` and in `str()`. DNS
    treats the two as one name, so nothing about reaching the target changes.

    **Credentials never render.** `str()` and `repr()` both emit the redacted
    form -- the password is removed rather than masked, so the output stays a
    valid, reusable connection string that cannot round-trip a wrong
    credential. `password` is excluded from the generated `repr`, so a value
    reaching a log line or a traceback frame does not leak it.

    A password may carry credential extras after a newline, exactly as
    :func:`parse_credentials` describes; `extras` holds them and `password` is
    just the password. The separator may be written raw, since characters
    `urlsplit` would silently delete are percent-encoded before parsing.
    """

    scheme: str
    host: str
    port: _ty.Optional[int] = None
    username: _ty.Optional[str] = None
    password: _ty.Optional[str] = _dc.field(default=None, repr=False)
    extras: _ty.Mapping[str, str] = _dc.field(default_factory=dict, repr=False)
    path: str = ""
    query: str = ""
    fragment: str = ""

    def __init__(
        self,
        value: _ty.Union[str, "ConnectionString"],
        *,
        scheme: _ty.Optional[str] = None,
        host: _ty.Optional[str] = None,
        port: _ty.Optional[int] = None,
        username: _ty.Optional[str] = None,
        password: _ty.Optional[str] = None,
        extras: _ty.Optional[_ty.Mapping[str, str]] = None,
        path: _ty.Optional[str] = None,
        query: _ty.Optional[str] = None,
        fragment: _ty.Optional[str] = None,
        defaults: _ty.Union["ConnectionString", _ty.Mapping[str, object], None] = None,
        default_ports: _ty.Optional[_ty.Mapping[str, int]] = None,
    ) -> None:
        overrides = {
            "scheme": scheme,
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "extras": extras,
            "path": path,
            "query": query,
            "fragment": fragment,
        }
        fallbacks = _as_defaults(defaults)
        unknown = set(fallbacks) - {field.name for field in _dc.fields(self)}
        if unknown:
            raise TypeError(f"unknown default field: {sorted(unknown)[0]}")

        if isinstance(value, ConnectionString):
            parsed_values: _ty.Dict[str, object] = {
                field.name: getattr(value, field.name) for field in _dc.fields(self)
            }
        else:
            # A scheme may come from the explicit argument or the defaults;
            # either lets a bare host parse, so resolve it before splitting.
            assumed = scheme or fallbacks.get("scheme")
            parsed = _split(value, _ty.cast(_ty.Optional[str], assumed))
            _reject_authority_control_characters(parsed)
            parsed_password, parsed_extras = None, {}
            if parsed.password is not None:
                parsed_password, parsed_extras = parse_credentials(
                    _unquote(parsed.password)
                )
            parsed_values = {
                "scheme": parsed.scheme.casefold() or None,
                "host": uri_hostname(parsed) or None,
                "port": parsed.port,
                "username": _unquote(parsed.username) if parsed.username else None,
                "password": parsed_password,
                "extras": dict(parsed_extras) or None,
                "path": parsed.path or None,
                "query": parsed.query or None,
                "fragment": parsed.fragment or None,
            }

        # Precedence: an explicit argument wins, then what the string carried,
        # then `defaults`. A default therefore fills a gap rather than
        # overriding something the caller actually wrote.
        resolved: _ty.Dict[str, object] = {}
        for name, override in overrides.items():
            for candidate in (override, parsed_values.get(name), fallbacks.get(name)):
                if candidate is not None:
                    resolved[name] = candidate
                    break
            else:
                resolved[name] = None

        if resolved["scheme"] is None:
            raise ValueError(
                f"connection string has no scheme: {value!r}; pass scheme= to set "
                "one, or defaults= to supply it only when the string omits it"
            )
        if resolved["port"] is None and default_ports:
            resolved["port"] = default_ports.get(
                _ty.cast(str, resolved["scheme"]).casefold()
            )

        object.__setattr__(self, "scheme", _ty.cast(str, resolved["scheme"]).casefold())
        object.__setattr__(self, "host", resolved["host"] or "")
        object.__setattr__(self, "port", resolved["port"])
        object.__setattr__(self, "username", resolved["username"])
        object.__setattr__(self, "password", resolved["password"])
        object.__setattr__(self, "extras", dict(resolved["extras"] or {}))
        object.__setattr__(self, "path", resolved["path"] or "")
        object.__setattr__(self, "query", resolved["query"] or "")
        object.__setattr__(self, "fragment", resolved["fragment"] or "")

    @property
    def authority(self) -> str:
        """The authority as it renders, without any password."""
        target = uri_host(self.host)
        if self.port is not None:
            target = f"{target}:{self.port}"
        if not self.username:
            return target
        return f"{_quote(self.username, safe='')}@{target}"

    def geturl(self) -> str:
        """The credential-free connection string, valid and reusable."""
        return _SplitResult(
            self.scheme, self.authority, self.path, self.query, self.fragment
        ).geturl()

    def __str__(self) -> str:
        return self.geturl()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.geturl()!r})"

    @property
    def qsl(self) -> _ty.List[_ty.Tuple[str, str]]:
        """Query pairs in order, keeping repeats."""
        return _parse_qsl(self.query, keep_blank_values=True)

    def query_val(self, name: str, default: _ty.Optional[str] = None):
        """The last value for `name`, or `default`. Last wins, as in a URI."""
        found = default
        for key, value in self.qsl:
            if key == name:
                found = value
        return found

    @property
    def is_local(self) -> bool:
        """Whether this names the local machine, without resolving anything.

        A string check, deliberately: this is called while a configuration is
        built, and that is network-free. Resolving would block, and would
        raise on a name that does not resolve -- so it could not even be used
        defensively.
        """
        return self.host.casefold() in _LOCAL_HOSTS

    def replace(self, **changes: object) -> "ConnectionString":
        """A copy with `changes` applied.

        Built field by field rather than through `dataclasses.replace`, which
        would re-invoke `__init__` -- and `__init__` parses a string, so it
        does not accept the field names.
        """
        names = {field.name for field in _dc.fields(self)}
        unknown = set(changes) - names
        if unknown:
            raise TypeError(f"unknown field: {sorted(unknown)[0]}")
        copy = object.__new__(type(self))
        for name in names:
            object.__setattr__(copy, name, changes.get(name, getattr(self, name)))
        return copy


def _as_defaults(
    defaults: _ty.Union["ConnectionString", _ty.Mapping[str, object], None],
) -> _ty.Dict[str, object]:
    """Normalize `defaults` to a mapping of field name to fallback value.

    A `ConnectionString` may be used as the defaults directly -- a partially
    filled one is the natural way to express "these settings unless the string
    says otherwise". Only fields it actually carries count as defaults, so its
    empty `path`/`query`/`fragment` do not override anything.
    """
    if defaults is None:
        return {}
    if isinstance(defaults, ConnectionString):
        return {
            field.name: value
            for field in _dc.fields(defaults)
            if (value := getattr(defaults, field.name))
        }
    return {name: value for name, value in defaults.items() if value is not None}


def _split(value: str, assumed_scheme: _ty.Optional[str]) -> _SplitResult:
    """Parse `value`, tolerating the shorthands people actually type.

    A bare `nas` or `nas:8443` has no scheme, and `urlsplit` reads neither as
    an authority -- `nas` becomes a *path*, and `nas:8443` becomes the scheme
    `nas`. Both are the same input with the scheme left off, so a scheme known
    from elsewhere in the call is prepended and the string re-parsed, rather
    than a second parser existing for the shorthand.

    `assumed_scheme` only makes the *authority* parseable; it does not decide
    the resulting scheme. The caller resolves that afterwards, so an explicit
    `scheme=` still wins over one written in the string.
    """
    encoded = _encode_stripped_characters(value)
    parsed = _urlsplit(encoded)
    if parsed.scheme and parsed.netloc:
        return parsed
    if assumed_scheme is None:
        return parsed
    # `nas:8443` parses as scheme `nas`; a numeric "path" is the port. Detect
    # that before treating the whole string as a host.
    if parsed.scheme and not parsed.netloc and parsed.path.isdigit():
        return _urlsplit(f"{assumed_scheme}://{encoded}")
    if not parsed.scheme:
        return _urlsplit(f"{assumed_scheme}://{encoded}")
    return parsed
