"""Parse, validate and redact connection URIs and their credentials."""

from __future__ import annotations


import re as _re
import typing as _ty
import unicodedata as _unicodedata
from pathlib import (
    PurePath as _PurePath,
    PurePosixPath as _PurePosixPath,
    PureWindowsPath as _PureWindowsPath,
)
from urllib.parse import (
    SplitResult as _SplitResult,
    parse_qsl as _parse_qsl,
    quote as _quote,
    unquote as _unquote,
    urlsplit as _urlsplit,
)


from pathapi import (
    Pathname as _Pathname,
    PosixPathname as _PosixPathname,
    WindowsPathname as _WindowsPathname,
)


def uri_host(host: str) -> str:
    """Bracket an IPv6 literal for use in a URI authority."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


#: Characters `urllib.parse` deletes from a URI before parsing it (WHATWG
#: requires the removal, and CPython adopted it for CVE-2022-0391). They are
#: removed *silently*, which is what makes them dangerous unencoded.
_URI_STRIPPED_CHARACTERS = {"\t": "%09", "\n": "%0A", "\r": "%0D"}


def _encode_stripped_characters(uri: str) -> str:
    """Percent-encode the characters `urlsplit` would silently delete.

    A raw tab, CR, or LF never survives `urlsplit`: it is removed before
    parsing, which changes what the URI means rather than failing. That is
    dangerous in two different ways, and they need different answers.

    In the **userinfo** it swallows data the caller meant to pass:
    `ssh://user:pw<LF>otp:1@host` would authenticate as `pwotp:1`, losing the
    credential extras. Writing the separator raw is the natural thing to do --
    a password read from a file or a prompt arrives with a real newline in it
    -- so encode it here and let it through.

    In the **authority** the same deletion rewrites the target:
    `ssh://host<LF>.other.example/` would resolve to `host.other.example`. No
    encoding makes that safe, because the caller cannot have meant a hostname
    containing a newline. `_reject_authority_control_characters` refuses those
    after parsing, once the userinfo has been separated out.

    Encoding happens before `urlsplit` sees the string, so the characters are
    preserved as data and `unquote` restores them when the password is read.
    """
    for character, encoded in _URI_STRIPPED_CHARACTERS.items():
        if character in uri:
            uri = uri.replace(character, encoded)
    return uri


def _refuse_uri(kind: str, value: object) -> None:
    """Refuse a URI where a config takes a host, without echoing its password."""
    if isinstance(value, str) and "://" in value:
        raise ValueError(
            f"{kind} takes a host, not a URI: {redact_uri(value)!r}. "
            "Use HostConfig(uri) or Host(uri) to build one from a URI."
        )


def reject_control_characters(text: str) -> None:
    """Refuse a C0, DEL or C1 control character, raw or percent-encoded.

    A hostname cannot legitimately contain one, and allowing it would let a URI
    that reads as one target resolve to another (TAB, CR and LF are deleted by
    `urlsplit`), or put a terminal escape into every log line built from the
    host.
    """
    try:
        decoded = _unquote(text, errors="strict")
    except UnicodeDecodeError:
        decoded = _unquote(text, errors="replace")
    for candidate in (text, decoded):
        if any(_unicodedata.category(char) == "Cc" for char in candidate):
            raise ValueError("connection URI host contains a control character")


def _reject_authority_control_characters(parsed: _SplitResult) -> None:
    """Refuse control characters in the host portion of an authority."""
    reject_control_characters(parsed.hostname or "")


def _refuse_cut_short_userinfo(parsed: _SplitResult) -> None:
    """Refuse a URI whose password ended the authority early.

    An unencoded `/`, `?` or `#` in a password ends the authority there, so
    `ssh://user:se?cret@host` reads as the netloc `user:se` and a query
    `cret@host`. The message never quotes the input: it holds password text.

    Only an authority that does not parse is refused: `user:se` has no valid
    port. `ssh://nas:22/mail/user@example.com` parses, so it is taken at face
    value -- the same test `redact_uri` uses to decide "well formed".
    """
    if "@" in parsed.netloc or "@" not in parsed.path + parsed.query + parsed.fragment:
        return
    try:
        parsed.port  # noqa: B018 -- the property raises for an invalid port
    except ValueError:
        pass
    else:
        return
    # Raised outside the handler: the port error, as `__context__`, quotes
    # the password text after the colon.
    raise ValueError(
        "connection URI has userinfo cut short: percent-encode '/', '?' "
        "and '#' in the password"
    )


_PARAMETER_NAME = _re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")


def _parameter_label(key: str, query: str) -> str:
    """`key` if it can be shown, else a fixed phrase: a query that was cut out
    of a password ('cret@host') is not a parameter name."""
    if _PARAMETER_NAME.fullmatch(key) and "@" not in query:
        return key
    return "(not shown: it is not a parameter name)"


def uri_hostname(parsed: _SplitResult) -> str:
    """The host as it was written, not as `urlsplit` case-folded it.

    `urlsplit().hostname` is lowercased by design -- DNS is case-insensitive,
    so folding is right for *resolution*. It is wrong for text a caller will
    see again. A config built from a URI stores this value and renders it back
    through `connection_uri`, so reading `.hostname` there would make the
    library echo a spelling the operator never typed, and `nasA` would reach
    logs and `HostInfo` as `nasa`.

    Use this in `_from_parsed_uri` wherever a host is stored. Presence checks
    (`if not parsed.hostname`) can keep using `.hostname` -- emptiness does not
    depend on case. Resolution is unaffected either way: DNS, SSH, and WinRM
    all treat the two spellings as one name.

    Returns `""` for a URI with no host, matching `hostname or ""`.

    `hostname` is a case-folded substring of the authority (after any
    userinfo), so locating it recovers the original text.
    """
    hostname = parsed.hostname or ""
    authority = parsed.netloc.rpartition("@")[2]
    index = authority.casefold().find(hostname)
    if index < 0:
        return hostname
    # Cut at the port separator rather than by `len(hostname)`: lowercasing is
    # not length-preserving (U+0130 folds to two characters), so the slice ran
    # past the host into the ':' of the port. The stored host gained a
    # trailing colon and was then rendered as a bracketed IPv6 literal, which
    # is both unresolvable and malformed.
    tail = authority[index:]
    closing = tail.find("]")
    if closing >= 0:
        # An IPv6 literal: `index` lands inside the brackets, because
        # `hostname` has none.
        return tail[:closing]
    port = tail.rfind(":")
    return tail if port < 0 else tail[:port]


def _rebuild_authority(parsed: _SplitResult, password: _ty.Optional[str]) -> str:
    """Rebuild a URI authority with `password` in place of the original."""
    host = uri_host(uri_hostname(parsed))
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    if not parsed.username:
        return host
    # `SplitResult.username` is the raw, still-percent-encoded text: urlsplit
    # does not decode it. Quoting it again turned `CORP%5Calice` into
    # `CORP%255Calice`, so a Windows domain or UPN login authenticated as the
    # literal encoded name -- and drifted further on every round trip. The
    # rebuild keeps the authority as written.
    userinfo = parsed.username
    if password is not None:
        userinfo = f"{userinfo}:{password}"
    return f"{userinfo}@{host}"


def _without_password(parsed: _SplitResult) -> _SplitResult:
    """Return `parsed` with any password removed from its authority."""
    return parsed._replace(netloc=_rebuild_authority(parsed, None))


def _with_username(parsed: _SplitResult, username: str) -> _SplitResult:
    """Return `parsed` (already password-free) with `username` as its user.

    `username` is the decoded name and is percent-encoded here, as the user
    part of a URI is; an empty one leaves no user at all.
    """
    bare = parsed._replace(netloc=parsed.netloc.rpartition("@")[2])
    host = _rebuild_authority(bare, None)
    if not username:
        return parsed._replace(netloc=host)
    return parsed._replace(netloc=f"{_quote(username, safe='')}@{host}")


def redact_uri(uri: str) -> str:
    """Strip any password from `uri`, leaving a valid, reusable URI.

    `scheme://user:secret@host` becomes `scheme://user@host`. The password is
    removed rather than masked: a placeholder would make the URI round-trip
    into a *wrong* credential if anything fed the rendered form back in, and
    would not be a real password anyway. What comes out is the same canonical,
    credential-free string a config renders, so it is safe to log, repr, or
    hand back to `HostConfig`.

    A URI with no password is returned unchanged.

    **A well-formed URI is never over-redacted.** When the URI parses, what
    `urlsplit` says is the authority *is* the authority, so an `@` anywhere
    else -- a mailbox in a path, `ssh://nas:22/mail/user@example.com` -- is
    left alone. Guessing is confined to input that does not parse, where
    there is nothing better to go on: see below.

    This never raises. It is meant for error messages and log records, where
    failing to render a diagnostic would be worse than rendering an odd one.
    Characters `urlsplit` would delete (tab, CR, LF) are percent-encoded first,
    the same as during dispatch, so a password written with a raw newline is
    still recognized and removed rather than partly surviving into the output.
    """
    try:
        parsed = _urlsplit(_encode_stripped_characters(uri))
        # Not dead code: `.port` is a property that RAISES, and touching it
        # is what decides "well formed" here. It catches both shapes that
        # matter in one place -- a non-numeric or out-of-range port, and an
        # unencoded `/`, `?` or `#` in a password, since the latter ends the
        # authority and leaves `user:secret` being read as host:port. So the
        # bad port IS the symptom of the hidden password.
        parsed.port  # noqa: B018
    except ValueError:
        pass  # Malformed. The textual fallback below is all that is left.
    else:
        if parsed.password is None:
            # Well formed and carrying no password: there is nothing to
            # strip, and the fallback must not run. It reads the LAST `@` in
            # the whole string as the userinfo delimiter, which would rewrite
            # `ssh://nas:22/mail/user@example.com` -- a URI with no
            # credentials at all -- into `ssh://nas@example.com`, naming a
            # different host.
            return uri
        try:
            return _without_password(parsed).geturl()
        except ValueError:
            # Not reachable through `.port`, which is already validated above;
            # kept because "never raises" is a contract this function is
            # called from inside exception handlers to honour.
            pass

    # Only a malformed URI reaches here. Read the userinfo textually: the last
    # `@` wins, as it does in a real authority. This can over-redact -- but
    # the text has already failed to say where its authority ended, so there
    # is no reading of it that is known to be right, and an odd diagnostic
    # beats a password in a log.
    head, separator, _rest = uri.partition("://")
    start = len(head) + len(separator)
    authority_end = uri.rfind("@")
    if authority_end <= start:
        return uri
    userinfo = uri[start:authority_end]
    username, colon, _secret = userinfo.partition(":")
    if not colon:
        return uri
    return uri[:start] + username + uri[authority_end:]


def parse_credentials(password: str) -> _ty.Tuple[str, _ty.Dict[str, str]]:
    """Split a password field into the password and any trailing extras.

    A newline separates the password from additional credential values, one
    per line, each `key:value`:

        "hunter2"                       -> ("hunter2", {})
        "hunter2\\notp:123456"           -> ("hunter2", {"otp": "123456"})
        "hunter2\\notp:123456\\nrealm:CORP" -> ("hunter2", {"otp": ..., "realm": ...})

    A bare name with no `:` is a flag -- it maps to the empty string, exactly
    as `name:` does:

        "hunter2\\ninteractive"           -> ("hunter2", {"interactive": ""})

    This is how a second factor reaches a transport through a single password
    field -- a URI's userinfo, an environment variable, a prompt -- without
    every caller inventing its own encoding. A newline is assumed not to be a
    valid password character, which is the same assumption pytruenas makes.

    Names are casefolded and stripped of surrounding whitespace. A value is
    everything after the first `:`, taken **verbatim** -- it may contain
    colons, and its leading and trailing whitespace is preserved, because a
    secret may legitimately begin or end with a space and silently trimming
    one would fail authentication with no visible cause. Blank lines are
    ignored.
    """
    # Split on any line ending: a value pasted from a file or a Windows prompt
    # arrives CRLF-terminated, and a trailing "\r" left on the password would
    # fail authentication with no visible cause.
    lines = password.splitlines()
    if len(lines) <= 1:
        return password, {}
    password, remainder = lines[0], lines[1:]
    extras: _ty.Dict[str, str] = {}
    for line in remainder:
        if not line.strip():
            continue
        # A line with no ":" is a bare flag; `partition` already yields "" for
        # its value, so flags and `name:` need no separate branch.
        key, _, value = line.partition(":")
        key = key.strip().casefold()
        if not key:
            raise ValueError("credential extra names must not be empty")
        extras[key] = value
    return password, extras


def strict_uri_query(
    parsed: _SplitResult,
    allowed: _ty.Iterable[str],
    *,
    repeatable: _ty.Iterable[str] = (),
) -> _ty.Dict[str, object]:
    """Parse one selected implementation's query without ambiguity.

    A repeated key is an error by default, so `?shell=a&shell=b` is not a
    coin flip; an unknown key is an error too, so a misspelt
    `?exectuor=ssh` fails here rather than as a missing capability later.

    `repeatable` names keys that are ORDERED LISTS rather than settings --
    `SystemConfig`'s `executor` and `path` -- and their values come back as
    a tuple in the order they were written. Declaring them here lets every
    config share this one parser.
    """
    repeated = frozenset(repeatable)
    query: _ty.Dict[str, object] = {}
    for key, value in _parse_qsl(parsed.query, keep_blank_values=True):
        if key in repeated:
            query[key] = _ty.cast(_ty.Tuple[str, ...], query.get(key, ())) + (value,)
        elif key in query:
            raise ValueError(
                f"duplicate connection parameter: {_parameter_label(key, parsed.query)}"
            )
        else:
            query[key] = value
    unknown = set(query) - set(allowed)
    if unknown:
        label = _parameter_label(sorted(unknown)[0], parsed.query)
        raise ValueError(f"unknown connection parameter: {label}")
    return query


def strict_uri_credentials(
    credentials: _ty.Mapping[str, object], allowed: _ty.Iterable[str]
) -> None:
    """Reject credentials which the selected implementation does not accept.

    A key whose value is `None` is not a credential -- nothing was supplied --
    so it is ignored rather than refused. Counting keys made
    `HostConfig("local:", password=None)` fail, which is exactly the shape a
    generic caller produces when it forwards an optional argument.
    """
    unknown = {name for name, value in credentials.items() if value is not None} - set(
        allowed
    )
    if unknown:
        raise ValueError(f"unknown credential argument: {sorted(unknown)[0]}")


def pathname_selection(value: object, *, allow_auto: bool) -> _ty.Any:
    """Normalise a config's `pathname` to a pure-path class (or `"auto"`).

    Accepts `"posix"`, `"windows"` (and `"auto"` where `allow_auto`), or a
    pure-path class with POSIX or Windows semantics. One vocabulary for every
    config that has the field, so a URI spelling and a Python value agree.
    """
    if isinstance(value, str):
        names: _ty.Dict[str, _ty.Any] = {
            "posix": _PosixPathname,
            "windows": _WindowsPathname,
        }
        if allow_auto:
            names["auto"] = "auto"
        try:
            return names[value]
        except KeyError:
            allowed = (
                "'auto', 'posix', or 'windows'"
                if allow_auto
                else ("'posix' or 'windows'")
            )
            raise ValueError(f"pathname must be {allowed}") from None
    if not isinstance(value, type) or not issubclass(value, (_Pathname, _PurePath)):
        suffix = " or 'auto'" if allow_auto else ""
        raise TypeError(f"pathname must be a pure-path class{suffix}")
    if value is _PurePath:
        raise TypeError(
            "bare PurePath uses the local OS; choose a concrete path flavor"
        )
    if not issubclass(value, (_PurePosixPath, _PureWindowsPath)):
        raise TypeError("pathname must use POSIX or Windows semantics")
    return value


def pathname_label(value: _ty.Any) -> str:
    """`"auto"`, `"posix"` or `"windows"`: the URI spelling of a selection."""
    if value == "auto":
        return "auto"
    return "windows" if issubclass(value, _PureWindowsPath) else "posix"


def _query_int(query: _ty.Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(query.get(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _seconds_text(value: float) -> str:
    """`20` for 20.0, so a URI keeps its whole-second spelling."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


_TRUE = frozenset(("1", "true", "yes", "on"))

_FALSE = frozenset(("0", "false", "no", "off"))


def _query_bool(query: _ty.Mapping[str, str], name: str, default: bool) -> bool:
    if name not in query:
        return default
    value = str(query[name]).casefold()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be one of 1/true/yes/on or 0/false/no/off")
