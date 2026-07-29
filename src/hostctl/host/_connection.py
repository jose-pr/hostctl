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
    people type on a command line -- so a `default_scheme` fills it in and a
    `default_ports` mapping fills a missing port:

        ConnectionString("nas", default_scheme="wss", default_ports={"wss": 443})

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
        default_scheme: _ty.Optional[str] = None,
        default_ports: _ty.Optional[_ty.Mapping[str, int]] = None,
    ) -> None:
        if isinstance(value, ConnectionString):
            for field in _dc.fields(self):
                object.__setattr__(self, field.name, getattr(value, field.name))
            return
        parsed = _split(value, default_scheme)
        _reject_authority_control_characters(parsed)
        password, extras = None, {}
        if parsed.password is not None:
            password, extras = parse_credentials(_unquote(parsed.password))
        scheme = parsed.scheme.casefold()
        port = parsed.port
        if port is None and default_ports:
            port = default_ports.get(scheme)
        object.__setattr__(self, "scheme", scheme)
        object.__setattr__(self, "host", uri_hostname(parsed))
        object.__setattr__(self, "port", port)
        object.__setattr__(
            self, "username", _unquote(parsed.username) if parsed.username else None
        )
        object.__setattr__(self, "password", password)
        object.__setattr__(self, "extras", dict(extras))
        object.__setattr__(self, "path", parsed.path)
        object.__setattr__(self, "query", parsed.query)
        object.__setattr__(self, "fragment", parsed.fragment)

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


def _split(value: str, default_scheme: _ty.Optional[str]) -> _SplitResult:
    """Parse `value`, tolerating the shorthands people actually type.

    A bare `nas` or `nas:8443` has no scheme, and `urlsplit` reads neither as
    an authority -- `nas` becomes a *path*, and `nas:8443` becomes the scheme
    `nas`. Both are the same input with the scheme left off, so a
    `default_scheme` is prepended and the string re-parsed rather than a
    second parser existing for the shorthand.
    """
    encoded = _encode_stripped_characters(value)
    parsed = _urlsplit(encoded)
    if parsed.scheme and parsed.netloc:
        return parsed
    if default_scheme is None:
        if not parsed.scheme:
            raise ValueError(
                f"connection string has no scheme and no default was given: {value!r}"
            )
        return parsed
    # `nas:8443` parses as scheme `nas`; a numeric "path" is the port. Detect
    # that before treating the whole string as a host.
    if parsed.scheme and not parsed.netloc and parsed.path.isdigit():
        return _urlsplit(f"{default_scheme}://{encoded}")
    if not parsed.scheme:
        return _urlsplit(f"{default_scheme}://{encoded}")
    return parsed
