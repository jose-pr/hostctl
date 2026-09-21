"""Protocol-independent host contracts and shared implementation helpers."""

from __future__ import annotations

import abc as _abc
import dataclasses as _dc
import importlib.metadata as _metadata
import subprocess as _subprocess
import threading as _threading
import typing as _ty
import unicodedata as _unicodedata
import types as _types
from pathlib import PurePath as _PurePath
from urllib.parse import (
    SplitResult as _SplitResult,
    parse_qsl as _parse_qsl,
    quote as _quote,
    unquote as _unquote,
    urlsplit as _urlsplit,
)

from pathlib_next import Path as HostPath, Pathname as _Pathname

from ..executor import (
    CaptureOutput,
    Environment,
    FileHandle,
    Input,
    PathLike,
    capture_streams,
    reject_stdin_conflict,
)

if _ty.TYPE_CHECKING:
    from ..executor import Executor, ExecutorCapability
    from ..process import Process, TerminalRequest
    from ..shell import Shell, ShellFlavour

#: One argv value. Everything reaching a transport is text, so a program or
#: argument may be spelled as a string, bytes, or a path object
#: interchangeably -- the spelling records how the caller held the value, not
#: what crosses the wire.
ExecValue = _ty.Union[str, bytes, _PurePath, _Pathname]


@_dc.dataclass(frozen=True)
class Exec:
    """One command executed directly, with no shell layer.

    ``Exec(program, *args)`` names a program and its argv. The program may be
    an absolute path or a bare name the target resolves through ``PATH``; a
    bare name has no other spelling, since a plain string is always shell text.

    This is the explicit marker for direct execution. A path used anywhere
    else is an ordinary value that stringifies, so several commands can be
    written without one of them silently becoming an executable::

        host.run(Exec("/bin/ls", "-l"))       # one direct command
        host.run(Exec("ls", "-l"))            # PATH-resolved, no shell
        host.run(Exec("/bin/a"), Exec("/bin/b"))   # two direct commands
        host.run(PurePosixPath("/bin/a"), PurePosixPath("/bin/b"))
                                              # two ordinary shell commands

    Arguments are argv values, never nested commands: a list or tuple raises
    ``TypeError`` rather than blurring argv and shell semantics.

    A deliberately non-iterable container. `ShellFlavour.command_text`
    dispatches structured commands on ``Iterable``, so an iterable marker
    would be quoted into an argv string instead of taking the direct branch.
    """

    program: ExecValue
    args: _ty.Tuple[ExecValue, ...]

    def __init__(self, program: ExecValue, *args: ExecValue) -> None:
        if not isinstance(program, (str, bytes, _PurePath, _Pathname)):
            raise TypeError("Exec program must be a str, bytes, or path value")
        for value in args:
            if isinstance(value, (tuple, list)):
                raise TypeError("direct command arguments must be scalar values")
            if not isinstance(value, (str, bytes, _PurePath, _Pathname)):
                raise TypeError(
                    "direct command arguments must be str, bytes, or path values"
                )
        object.__setattr__(self, "program", program)
        object.__setattr__(self, "args", tuple(args))


Command = _ty.Union[str, PathLike, Exec, _ty.Sequence[object]]


def starts_direct_command(
    cmds: _ty.Sequence[Command],
) -> _ty.Optional[_ty.Tuple[ExecValue, _ty.Tuple[object, ...]]]:
    """Split an :class:`Exec` call into one executable and its argv arguments.

    Returns ``None`` unless the call is exactly one `Exec`. Direct execution
    replaces the whole command list rather than joining with anything, so an
    `Exec` alongside other commands is rejected: there is no shell to join
    them with, and silently running only one would lose the rest.
    """
    if not cmds:
        return None
    marked = [value for value in cmds if isinstance(value, Exec)]
    if not marked:
        return None
    if len(cmds) > 1:
        raise TypeError(
            "a direct Exec command cannot be combined with other commands; "
            "run it in its own call"
        )
    command = marked[0]
    return command.program, command.args


@_dc.dataclass(frozen=True)
class HostInfo:
    """Normalized system information; unavailable values remain ``None``."""

    hostname: _ty.Optional[str] = None
    os_family: _ty.Optional[str] = None
    os_name: _ty.Optional[str] = None
    os_version: _ty.Optional[str] = None
    architecture: _ty.Optional[str] = None


def parse_host_info(output: _ty.Union[bytes, str, None]) -> HostInfo:
    """Parse newline-delimited ``HostInfo`` fields from a transport response."""
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    values = {}
    for line in (output or "").splitlines():
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if separator and key in HostInfo.__dataclass_fields__ and value:
            values[key] = value
    if "os_family" in values:
        values["os_family"] = normalize_os_family(values["os_family"])
    return HostInfo(**values)


def normalize_os_family(value: _ty.Optional[str]) -> _ty.Optional[str]:
    """Normalize a directly reported OS family without inferring one."""
    if not value:
        return None
    normalized = value.casefold()
    aliases = {
        "win32nt": "windows",
        "windows": "windows",
        "linux": "linux",
        "darwin": "macos",
        "macos": "macos",
    }
    return aliases.get(normalized, normalized)


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


def _reject_authority_control_characters(parsed: _SplitResult) -> None:
    """Refuse control characters in the host portion of an authority.

    These arrive percent-encoded (see `_encode_stripped_characters`), so the
    check is against the encoded spelling -- `urlsplit` leaves `%0A` in the
    hostname verbatim. A hostname cannot legitimately contain one, and
    allowing it would let a URI that reads as one target resolve to another.
    """
    host = parsed.hostname or ""
    if not host:
        return
    upper = host.upper()
    if any(encoded in upper for encoded in _URI_STRIPPED_CHARACTERS.values()):
        raise ValueError(
            "connection URI host contains a control character; only the "
            "userinfo may carry one"
        )
    # Every other control character too, in either spelling. Only TAB, CR and
    # LF were checked -- the three `urlsplit` deletes -- so NUL, ESC, VT and
    # DEL travelled into `config.host` and from there into every log line and
    # error message built from it, where an ESC sequence is terminal-escape
    # injection into whatever reads the log.
    if any(_unicodedata.category(char) == "Cc" for char in host):
        raise ValueError("connection URI host contains a control character")
    upper_encoded = host.upper()
    for code in list(range(0x20)) + [0x7F]:
        if f"%{code:02X}" in upper_encoded:
            raise ValueError("connection URI host contains a control character")


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


def redact_uri(uri: str) -> str:
    """Strip any password from `uri`, leaving a valid, reusable URI.

    `scheme://user:secret@host` becomes `scheme://user@host`. The password is
    removed rather than masked: a placeholder would make the URI round-trip
    into a *wrong* credential if anything fed the rendered form back in, and
    would not be a real password anyway. What comes out is the same canonical,
    credential-free string a config renders, so it is safe to log, repr, or
    hand back to `HostConfig`.

    A URI with no password is returned unchanged.

    This never raises. It is meant for error messages and log records, where
    failing to render a diagnostic would be worse than rendering an odd one.
    Characters `urlsplit` would delete (tab, CR, LF) are percent-encoded first,
    the same as during dispatch, so a password written with a raw newline is
    still recognized and removed rather than partly surviving into the output.
    """
    try:
        parsed = _urlsplit(_encode_stripped_characters(uri))
        password = parsed.password
    except ValueError:
        # A malformed IPv6 authority. The textual fallback below still strips
        # the userinfo, which is the whole job.
        password = None
    else:
        if password is not None:
            try:
                return _without_password(parsed).geturl()
            except ValueError:
                # `_rebuild_authority` reads `parsed.port`, which raises for a
                # non-numeric or out-of-range one. This function is called
                # from inside exception handlers to format a diagnostic, so
                # raising here would replace the real error with this one.
                pass

    # `urlsplit` finds no password when one contains an unencoded `/`, `?` or
    # `#`: those end the authority, so the secret lands in the path or query
    # and the "redacted" form used to be the input in full. Randomly generated
    # passwords routinely contain `/`.
    #
    # Fall back to reading the userinfo textually. The last `@` wins, as it
    # does in a real authority. This can over-redact a path that contains `@`
    # -- deliberately: an odd diagnostic beats a password in a log, which is
    # the trade-off this function already documents.
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


class _HostConfigMeta(_abc.ABCMeta):
    def __call__(cls, *args: object, **options: object) -> HostConfig:
        if cls is HostConfig:
            if len(args) != 1 or not isinstance(args[0], str):
                raise TypeError(
                    "HostConfig() requires one connection string positional argument"
                )
            return cls._from_uri(args[0], **options)
        return super().__call__(*args, **options)


class HostConfig(_abc.ABC, metaclass=_HostConfigMeta):
    """Secret-safe connection configuration and extensible URI dispatch."""

    #: Credential names `HostConfig(uri, **credentials)` may pass to this
    #: config. Declaring it makes dispatch reject anything else *before*
    #: construction, so a typo (`passwrd=`) fails loudly instead of silently
    #: producing a config with no password. `None` (the default) skips the
    #: check, for configs that validate credentials themselves.
    uri_credentials: _ty.ClassVar[_ty.Optional[_ty.Tuple[str, ...]]] = None

    _uri_schemes: _ty.ClassVar[_ty.Tuple[str, ...]] = ()
    _uri_registry_cache: _ty.ClassVar[
        _ty.Optional[_ty.Tuple[_ty.Type["HostConfig"], ...]]
    ] = None
    _uri_entry_points: _ty.ClassVar[_ty.Optional[tuple[object, ...]]] = None
    _uri_plugin_failures: _ty.ClassVar[dict[str, Exception]] = {}
    _uri_registry_generation: _ty.ClassVar[int] = 0
    _uri_registry_lock: _ty.ClassVar[_threading.RLock] = _threading.RLock()

    #: Lifecycle state lives on the CLASS as well, because `HostConfig` is a
    #: documented extension point and a subclass with its own `__init__` that
    #: does not call `super().__init__()` -- the shape this project's own
    #: tests use -- otherwise dispatched and rendered fine and then failed
    #: `with config as host:` with a bare AttributeError about a private
    #: attribute.
    _opened_host: _ty.Optional["Host"] = None
    _lifecycle_lock: _ty.Optional["_threading.Lock"] = None

    def __init__(self) -> None:
        self._opened_host: _ty.Optional[Host] = None
        self._lifecycle_lock = _threading.Lock()

    def _lifecycle(self) -> "_threading.Lock":
        """The instance's lifecycle lock, created on first use if needed."""
        lock = self.__dict__.get("_lifecycle_lock")
        if lock is None:
            with HostConfig._uri_registry_lock:
                lock = self.__dict__.get("_lifecycle_lock")
                if lock is None:
                    lock = _threading.Lock()
                    object.__setattr__(self, "_lifecycle_lock", lock)
        return lock

    def __init_subclass__(
        cls,
        *,
        schemes: _ty.Iterable[str] = (),
        **kwargs: object,
    ) -> None:
        super().__init_subclass__(**kwargs)
        cls._uri_schemes = tuple(scheme.casefold() for scheme in schemes)
        HostConfig._refresh_uri_registry()

    @property
    def scheme(self) -> str:
        return _urlsplit(self.connection_uri).scheme.casefold()

    def __str__(self) -> str:
        return self.connection_uri

    @property
    @_abc.abstractmethod
    def connection_uri(self) -> str:
        """Credential-safe canonical connection URI."""

    @_abc.abstractmethod
    def _create_host(self) -> Host:
        """Create the operational host represented by this configuration."""

    def open(self) -> Host:
        """Return a host context manager for this configuration."""
        return self._create_host()

    def __enter__(self) -> Host:
        with self._lifecycle():
            if self._opened_host is not None:
                raise RuntimeError("host configuration is already open")
            host = self._create_host()
            self._opened_host = host
        try:
            return host.__enter__()
        except BaseException:
            try:
                host.close()
            except BaseException:
                pass
            finally:
                with self._lifecycle():
                    if self._opened_host is host:
                        self._opened_host = None
            raise

    def __exit__(
        self,
        exc_type: _ty.Optional[_ty.Type[BaseException]],
        exc_value: _ty.Optional[BaseException],
        traceback: _ty.Optional[_types.TracebackType],
    ) -> _ty.Optional[bool]:
        with self._lifecycle():
            host, self._opened_host = self._opened_host, None
        if host is not None:
            return host.__exit__(exc_type, exc_value, traceback)
        return False

    @classmethod
    def _from_uri(
        cls,
        uri: str,
        **credentials: object,
    ) -> HostConfig:
        """Dispatch a connection URI to a registered configuration.

        A `scheme://user:secret@host` URI is accepted: the password is
        extracted into the credential arguments and stripped from the parsed
        authority, so it is never stored where `connection_uri` or `repr()`
        would render it. Use :func:`redact_uri` before showing a URI that may
        still carry one.

        The password field is parsed by :func:`parse_credentials`, so a newline
        separates trailing `key:value` extras -- an OTP or other second factor
        travels through the same field. The newline may be written raw: it is
        percent-encoded before parsing (`urlsplit` would otherwise delete it),
        and decoded again when the password is read. A control character in the
        *host* is still refused, because no encoding makes that meaningful.
        """
        parsed = _urlsplit(_encode_stripped_characters(uri))
        _reject_authority_control_characters(parsed)
        if parsed.fragment:
            raise ValueError("connection URI fragments are not supported")
        if parsed.password is not None:
            # `scheme://user:secret@host` is a valid URI, so accept it: extract
            # the password into the credential arguments and strip it from the
            # parsed authority. The password therefore never reaches a config
            # field that `connection_uri`/`repr` render, which is what keeps
            # the canonical form credential-free.
            if credentials.get("password") is not None:
                raise ValueError(
                    "password given both in the connection URI and as an argument"
                )
            password, extras = parse_credentials(_unquote(parsed.password))
            credentials["password"] = password
            for key, value in extras.items():
                if key in credentials:
                    raise ValueError(
                        f"credential {key!r} given both in the connection URI "
                        "and as an argument"
                    )
                credentials[key] = value
            parsed = _without_password(parsed)
        matches = [
            implementation
            for implementation in cls._uri_implementations(parsed.scheme)
            if implementation._matches_uri(parsed)
        ]
        if not matches:
            raise ValueError(
                f"unsupported host scheme: {parsed.scheme.casefold() or '<missing>'}"
            )
        if len(matches) > 1:
            names = ", ".join(item.__name__ for item in matches)
            raise ValueError(f"ambiguous host URI matched: {names}")
        implementation = matches[0]
        # Fail closed on an unknown credential *here*, so a config gets the
        # safe behaviour by declaring `uri_credentials` rather than by
        # remembering to call a helper. A typo like `passwrd=` would otherwise
        # build a config with no password and no complaint, surfacing much
        # later as an unexplained authentication failure.
        if implementation.uri_credentials is not None:
            strict_uri_credentials(credentials, implementation.uri_credentials)
        return implementation._from_parsed_uri(parsed, **credentials)

    @classmethod
    def supported_credentials(cls, uri: str) -> _ty.Optional[_ty.Tuple[str, ...]]:
        """Credential names the implementation selected by `uri` accepts.

        `None` means the implementation declares no whitelist and takes
        whatever it is given. Use this for an *ambient* credential -- one
        supplied for a session rather than for a single call, such as the
        CLI's `HOSTCTL_PASSWORD` -- which should be offered only where it can
        be used. An explicitly passed credential still fails closed, because
        there a name the config does not know is a typo worth reporting.
        """
        parsed = _urlsplit(_encode_stripped_characters(uri))
        matches = [
            implementation
            for implementation in cls._uri_implementations(parsed.scheme)
            if implementation._matches_uri(parsed)
        ]
        if len(matches) != 1:
            return None
        return matches[0].uri_credentials

    @classmethod
    def _matches_uri(cls, parsed: _SplitResult) -> bool:
        """Whether this implementation accepts a parsed URI."""
        return parsed.scheme.casefold() in cls._uri_schemes

    @classmethod
    def _from_parsed_uri(
        cls, parsed: _SplitResult, **credentials: object
    ) -> HostConfig:
        """Construct from a URI selected by :meth:`_matches_uri`."""
        raise NotImplementedError(f"{cls.__name__} does not implement URI construction")

    @classmethod
    def _refresh_uri_registry(cls) -> None:
        """Clear URI implementation discovery for tests and newly loaded plugins."""
        with HostConfig._uri_registry_lock:
            HostConfig._uri_registry_generation += 1
            HostConfig._uri_registry_cache = None
            HostConfig._uri_entry_points = None
            HostConfig._uri_plugin_failures = {}

    @classmethod
    def _uri_implementations(
        cls,
        requested_scheme: _ty.Optional[str] = None,
    ) -> _ty.Tuple[_ty.Type[HostConfig], ...]:
        scheme = requested_scheme.casefold() if requested_scheme else None

        # Every import below happens OUTSIDE `_uri_registry_lock`. Holding it
        # across an import inverted the lock order against
        # `__init_subclass__`, which takes the registry lock from inside a
        # module body: one thread held the registry lock and waited for a
        # module lock while another held that module lock and waited for the
        # registry lock. Python's import deadlock detection only covers module
        # locks, so both hung forever.
        with HostConfig._uri_registry_lock:
            needs_builtins = HostConfig._uri_registry_cache is None
        if needs_builtins:
            from . import (
                container as _container,
                _local,
                qemu as _qemu,
                serial as _serial,
                _ssh,
                _winrm,
            )

            del _container, _local, _qemu, _serial, _ssh, _winrm

        with HostConfig._uri_registry_lock:
            while HostConfig._uri_registry_cache is None:
                generation = HostConfig._uri_registry_generation
                discovered = list(_recursive_subclasses(HostConfig))
                if generation != HostConfig._uri_registry_generation:
                    continue
                HostConfig._uri_registry_cache = tuple(
                    item for item in discovered if item._uri_schemes
                )
            if HostConfig._uri_entry_points is None:
                # Reading metadata imports nothing, so it stays under the lock.
                points = _metadata.entry_points()
                if hasattr(points, "select"):
                    points = points.select(group="hostctl.configs")
                elif hasattr(points, "get"):
                    points = points.get("hostctl.configs", ())
                else:
                    points = tuple(
                        item
                        for item in points
                        if getattr(item, "group", None) == "hostctl.configs"
                    )
                HostConfig._uri_entry_points = tuple(points)
            candidates = list(HostConfig._uri_registry_cache)
            entry_points = HostConfig._uri_entry_points
            failure = (
                HostConfig._uri_plugin_failures.get(scheme)
                if scheme is not None
                else None
            )

        if failure is not None:
            # A FRESH exception. Re-raising the cached instance appended the
            # current frames to its traceback every time, so a service that
            # retried a broken plugin's scheme per request grew the traceback
            # -- and every frame's locals, including credentials passed as
            # dispatch kwargs -- without bound, for the life of the process.
            raise RuntimeError(
                f"hostctl.configs entry point for scheme {scheme!r} failed to load"
            ) from failure
        if scheme is not None:
            known = {
                value
                for item in candidates
                for value in getattr(item, "_uri_schemes", ())
            }
            for entry_point in entry_points:
                name = str(getattr(entry_point, "name", "")).casefold()
                # The entry point's NAME matching the scheme is the fast
                # path, not the rule. A plugin may declare several schemes
                # under one entry point (`schemes=("plug", "plug+tls")`), and
                # matching only on the name made its secondary schemes work
                # or fail depending on whether something had already
                # dispatched the primary one in this process. An unknown
                # scheme now loads the remaining entry points too; each load
                # happens once, and a plugin that matches nothing simply
                # leaves the scheme unsupported as before.
                if name != scheme and scheme in known:
                    continue
                try:
                    # The plugin import that must not hold the registry lock.
                    implementation = entry_point.load()
                    if not isinstance(implementation, type) or not issubclass(
                        implementation, HostConfig
                    ):
                        raise TypeError(
                            f"hostctl.configs entry point {name!r} "
                            "must load a HostConfig subclass"
                        )
                except Exception as exc:
                    with HostConfig._uri_registry_lock:
                        HostConfig._uri_plugin_failures[name] = exc
                    import warnings

                    warnings.warn(
                        f"unable to load hostctl.configs entry point {name!r}: {exc}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    raise
                candidates.append(implementation)
                if scheme in getattr(implementation, "_uri_schemes", ()):
                    break
        return tuple(
            item
            for item in dict.fromkeys(candidates)
            if issubclass(item, cls) and item._uri_schemes
        )


class _HostMeta(_abc.ABCMeta):
    def __call__(cls, *args: object, **options: object) -> Host:
        if cls is Host:
            if len(args) != 1 or not isinstance(args[0], str):
                raise TypeError(
                    "Host() requires one connection string positional argument"
                )
            config = HostConfig._from_uri(args[0], **options)
            return config._create_host()
        return super().__call__(*args, **options)


class _ShellAccessor:
    """Expose `host.shell` as both the shell itself and a configuring call.

    `host.shell` must keep working as the bound `Shell` -- `host.shell.run()`,
    `host.shell.session()`, `with host.shell as session:` all predate this.
    `host.shell(cwd=...)` must additionally return a shell carrying defaults.

    A plain property cannot do both, and overloading `Shell.__call__` is not an
    option: that is the `Executor` protocol's execute entry point, so making it
    mean "configure" when called without a command would make every executor
    call site ambiguous. This descriptor instead returns the built shell for
    attribute access and, because a `Shell` is itself callable, routes a
    keyword-only call through `Shell.configure`.
    """

    def __init__(self, build):
        self._build = build
        self.__doc__ = build.__doc__

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return _ConfigurableShell(self._build(instance))


class _ConfigurableShell:
    """A `Shell` proxy whose keyword-only call returns a configured shell."""

    __slots__ = ("_shell",)

    def __init__(self, shell):
        object.__setattr__(self, "_shell", shell)

    def __call__(self, *args, **options):
        if args:
            # A positional argument means the caller is using the `Executor`
            # protocol (`shell(command, ...)`); defer to the real shell.
            return self._shell(*args, **options)
        return self._shell.configure(**options)

    def __getattr__(self, name):
        return getattr(self._shell, name)

    def __setattr__(self, name, value):
        # `host.shell` builds a NEW `Shell` on every attribute access, so an
        # assignment here reached an object discarded at the end of the
        # expression: `host.shell.env = {"TZ": "UTC"}` appeared to work and
        # changed nothing. Say so instead, and name the spelling that does
        # work -- `Shell.cwd`/`env`/`encoding`/`errors` are ordinary public
        # attributes, so trying to set them is a reasonable thing to do.
        if name in ("cwd", "env", "encoding", "errors"):
            raise AttributeError(
                f"host.shell is rebuilt on every access, so setting {name!r} "
                f"here would be discarded; use host.shell({name}=...) to get "
                "a configured shell"
            )
        setattr(self._shell, name, value)

    def __enter__(self):
        return self._shell.__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        return self._shell.__exit__(exc_type, exc_value, traceback)

    def __repr__(self):
        return repr(self._shell)

    @property
    def __class__(self):
        # `Host.shell` is annotated `-> Shell` and this proxy forwards every
        # attribute to one, but `isinstance(host.shell, Shell)` was False,
        # which is the one question a caller asks about a documented return
        # type. Answering with the wrapped shell's type keeps the annotation
        # honest without turning the proxy into a subclass.
        return type(self._shell)


class Host(_abc.ABC, metaclass=_HostMeta):
    """Protocol-independent operational interface to a machine."""

    config: HostConfig

    @property
    def scheme(self) -> str:
        return self.config.scheme

    @property
    def connection_uri(self) -> str:
        return self.config.connection_uri

    @property
    def shell_flavour(self) -> ShellFlavour:
        """The explicitly known shell language used by this host."""
        raise NotImplementedError(
            f"{type(self).__name__} does not identify a shell flavour"
        )

    @property
    def executor(self) -> Executor[_subprocess.CompletedProcess]:
        """The command executor used when binding this host's shell."""
        host = self

        class _HostExecutor:
            executor_capabilities = host.executor_capabilities

            def __call__(self, command, *args, **options):
                # `Executor.__call__(command, *args)` is one program plus
                # argv. Forwarding the argv as further positionals made
                # `host.run()` read them as SEPARATE top-level commands, so
                # `executor("grep", "-r", "needle")` ran `grep; -r; needle`.
                if args:
                    return host.run(Exec(command, *args), **options)
                return host.run(command, **options)

        return _HostExecutor()

    @property
    def executor_capabilities(self) -> _ty.FrozenSet[ExecutorCapability]:
        """Native context/argument features of the underlying executor.

        Derived from the host's own executor so a host that does not compose
        providers still reports truthfully. `Host.executor`'s default wrapper
        reads this property, so it is skipped here to avoid recursing; a host
        that overrides `executor` with a real executor reports that executor's
        capabilities.
        """
        executor = type(self).executor
        if executor is Host.executor:
            return frozenset()
        return frozenset(getattr(self.executor, "executor_capabilities", ()))

    @_ShellAccessor
    def shell(self) -> Shell[_subprocess.CompletedProcess]:
        """A shell bound to this host's executor.

        Used directly -- `host.shell.run(...)`, `host.shell.session(...)`, or
        `with host.shell as session:` -- it carries no defaults.

        Called with keywords -- `host.shell(cwd="/srv/app", env={"TZ": "UTC"})`
        -- it returns a shell carrying those defaults, applied to every later
        `run`, `execute`, and `session` that does not pass its own value.
        `env` merges per key; `cwd`, `encoding`, and `errors` override.
        """
        from ..shell import Shell

        return Shell(self.shell_flavour, self)

    def _run_selector(self) -> _ty.Optional[object]:
        """The :class:`~hostctl.provider.ProviderSelector` backing :meth:`run`.

        ``None`` for a host that does not select a command provider at all.
        The default finds the attribute used by the provider-composed system
        hosts; assemblies that name theirs differently override this.
        """
        return getattr(self, "_executor_selector", None)

    @property
    def last_selection(self) -> _ty.Tuple[_ty.Dict[str, object], ...]:
        """The redacted provider trace for the most recent :meth:`run`.

        The run-side counterpart of ``CompositePosixPath.selection_trace``.
        Entries appear in provider precedence order and carry ``provider``,
        ``availability``, ``reason``, ``capabilities``, ``chosen``,
        ``generation``, ``policy``, and ``pin``.

        The trace accumulates across the failover attempts of a single
        ``run()``, so a call that fell through from one provider to another
        reports every provider tried and every refusal reason -- including on
        the very call that suffered them.

        Empty for a host whose ``run()`` does not select between providers
        (``QemuHost``, ``SerialHost``), and empty before the first ``run()``.
        Values are redacted on the way in; see
        :meth:`~hostctl.provider.ProviderSelector.redact` for the limits of
        that guarantee.
        """
        selector = self._run_selector()
        if selector is None:
            return ()
        return getattr(selector, "trace", ())

    @property
    @_abc.abstractmethod
    def capabilities(self) -> _ty.FrozenSet[str]:
        """Operations supported by this host."""

    @_abc.abstractmethod
    def info(self) -> HostInfo:
        """Return normalized system information without inferred values."""

    def connect(self) -> None:
        """Open transport resources; local/default implementations are no-op."""

    def close(self) -> None:
        """Close transport resources; must be safe to call repeatedly."""

    def __enter__(self) -> Host:
        try:
            self.connect()
        except BaseException:
            try:
                self.close()
            except BaseException:
                pass
            raise
        return self

    def __exit__(
        self,
        exc_type: _ty.Optional[_ty.Type[BaseException]],
        exc_value: _ty.Optional[BaseException],
        traceback: _ty.Optional[_types.TracebackType],
    ) -> bool:
        self.close()
        return False

    def path(self, *segments: PathLike, backend: _ty.Optional[str] = None) -> HostPath:
        """Return a pathlib-compatible path for this host."""
        raise NotImplementedError(
            f"{type(self).__name__} does not provide the 'path' capability"
        )

    def spawn(
        self,
        *cmds: Command,
        executable: _ty.Optional[str] = None,
        cwd: _ty.Optional[PathLike] = None,
        env: _ty.Optional[Environment] = None,
        terminal: TerminalRequest = None,
        encoding: _ty.Optional[str] = None,
        errors: _ty.Optional[str] = None,
    ) -> Process:
        """Start a persistent process controlled through a synchronous facade."""
        raise NotImplementedError(
            f"{type(self).__name__} does not provide the 'spawn' capability"
        )

    def run(
        self,
        *cmds: Command,
        bufsize: int = -1,
        executable: _ty.Optional[str] = None,
        stdin: _ty.Optional[FileHandle] = None,
        stdout: _ty.Optional[FileHandle] = None,
        stderr: _ty.Optional[FileHandle] = None,
        cwd: _ty.Optional[PathLike] = None,
        env: _ty.Optional[Environment] = None,
        capture_output: CaptureOutput = True,
        check: bool = True,
        encoding: _ty.Optional[str] = None,
        errors: _ty.Optional[str] = None,
        input: Input = None,
        timeout: _ty.Optional[float] = None,
        text: _ty.Optional[bool] = None,
    ) -> _subprocess.CompletedProcess:
        """Run commands and return a subprocess-compatible result.

        A string is verbatim shell text.  A tuple/list is one quoted argv
        command.  :class:`Exec` is the only direct-execution spelling: it runs
        one program with an argv and no shell layer, and it cannot be combined
        with other commands.  A path anywhere else is an ordinary value that
        stringifies.  Multiple top-level commands are joined by the selected
        shell's command separator.

        Two defaults differ from :func:`subprocess.run` deliberately, and
        both are the opposite of it: ``check`` is ``True``, so a non-zero
        status raises :class:`subprocess.CalledProcessError` unless the
        caller passes ``check=False``; and ``capture_output`` is ``True``, so
        output is captured rather than inherited by a process that may have
        no console.  The result type and every other keyword do follow
        ``subprocess``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not provide the 'run' capability"
        )


def strict_uri_query(
    parsed: _SplitResult, allowed: _ty.Iterable[str]
) -> _ty.Dict[str, str]:
    """Parse one selected implementation's query without ambiguity."""
    query = {}
    for key, value in _parse_qsl(parsed.query, keep_blank_values=True):
        if key in query:
            raise ValueError(f"duplicate connection parameter: {key}")
        query[key] = value
    unknown = set(query) - set(allowed)
    if unknown:
        raise ValueError(f"unknown connection parameter: {sorted(unknown)[0]}")
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


def _query_int(query: _ty.Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(query.get(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _recursive_subclasses(
    base: _ty.Type[HostConfig],
) -> _ty.Iterator[_ty.Type[HostConfig]]:
    for subclass in base.__subclasses__():
        yield subclass
        yield from _recursive_subclasses(subclass)
