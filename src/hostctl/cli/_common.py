"""What every command shares: credentials, the host operand, and output."""

from __future__ import annotations

import contextlib
import dataclasses
import getpass
import io
import pathlib
import re
import sys
import threading
import typing
from urllib.parse import urlsplit

from duho import Arg, Cmd, LoggingArgs, Meta, running_mode, value_sources

from ..exceptions import OperationNotStartedError
from ..host._host import Host
from ..host._config import HostConfig
from pathapi import Path
from ..uri._parse import redact_uri

_URI_OPERAND = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

#: The status for everything the command line itself reports, kept apart from
#: the 0-124 a remote command can return through `run`.
FAILED = 125


def write(stream: typing.Union[typing.TextIO, typing.BinaryIO], value) -> None:
    """Copy command output to `stream` unchanged: bytes as bytes, text as text."""
    if value is None:
        return
    if isinstance(value, bytes):
        binary = getattr(stream, "buffer", None)
        if binary is not None:
            binary.write(value)
        elif isinstance(stream, io.TextIOBase):
            # A text-only stream -- a tool call captures stderr in one -- has
            # no bytes side to write to.
            stream.write(value.decode("utf-8", "backslashreplace"))
        else:
            stream.write(value)
    else:
        stream.write(str(value))
    flush = getattr(stream, "flush", None)
    if flush:
        flush()


def display_name(name: str) -> str:
    """`name` with backslashes and non-printable characters written as escapes.

    A peer chooses these bytes: an unescaped newline forges a listing line and
    an ESC sequence drives the operator's terminal. A lone surrogate is a byte
    the filesystem layer could not decode, written as that byte.
    """
    out = []
    for char in name:
        code = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif 0xDC80 <= code <= 0xDCFF:
            out.append(f"\\x{code - 0xDC00:02x}")
        elif char.isprintable():
            out.append(char)
        elif code < 0x100:
            out.append(f"\\x{code:02x}")
        elif code < 0x10000:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    return "".join(out)


def usable_credentials(uri: str, credentials: dict) -> dict:
    """Keep only what the target scheme can accept.

    `HOSTCTL_PASSWORD` is ambient -- exported for a shell session, not for one
    command -- so a scheme that declares no password (`local:`) must not fail
    on it. A credential a config does not know, passed explicitly, still does.
    """
    try:
        accepted = HostConfig.supported_credentials(uri)
    except ValueError:
        return credentials
    if accepted is None:
        return credentials
    return {key: value for key, value in credentials.items() if key in accepted}


#: Keys of `Credentialed.credentials()` that are not credentials: the
#: password came from the environment, and what to call when one is needed.
_AMBIENT = "_ambient"
_ASK = "_ask"


def offered(uri: str, credentials: dict) -> dict:
    """The keyword credentials to open `uri` with.

    An explicit `-u` or `-p` the scheme takes no such credential for raises
    `ValueError`; `HOSTCTL_PASSWORD` is offered only where it is accepted.
    The prompt (or the error standing in for it) goes only to a scheme that
    takes a password and to a URI that carries none of its own.
    """
    accepted = _accepted(uri)
    scheme = uri.partition(":")[0]

    def takes(name: str) -> bool:
        return accepted is None or name in accepted

    found: typing.Dict[str, object] = {}
    if credentials.get("username") is not None:
        if not takes("username"):
            raise ValueError(f"{scheme}: takes no username (-u)")
        found["username"] = credentials["username"]
    password = credentials.get("password")
    if password is not None:
        if takes("password"):
            found["password"] = password
        elif not credentials.get(_AMBIENT):
            raise ValueError(f"{scheme}: takes no password (-p)")
    elif (
        credentials.get(_ASK) is not None
        and accepted is not None
        and "password" in accepted
        and not _uri_has_password(uri)
    ):
        found["password"] = credentials[_ASK]
    return found


def with_ssh_config(config: HostConfig, path: str) -> HostConfig:
    """`config` reading the ssh_config file `path`, or none for `''`.

    On the command line the value is always a file name, never config text.
    A scheme with no ssh_config raises `ValueError`, as `-u` does.
    """
    from ..transports.ssh._config import SSHConfig

    if not isinstance(config, SSHConfig):
        scheme = config.connection_uri.partition(":")[0]
        raise ValueError(f"{scheme}: takes no ssh config (--ssh-config)")
    return dataclasses.replace(config, ssh_config=pathlib.Path(path) if path else None)


def _accepted(uri: str) -> typing.Optional[typing.Tuple[str, ...]]:
    try:
        return HostConfig.supported_credentials(uri)
    except ValueError:
        return None


def _port_colon(value: str, scheme_end: int, authority_end: int) -> int:
    """Index of the authority's port colon, or ``-1`` when it has none.

    The port colon is the first colon of the host part: userinfo colons sit
    before the ``@``, and an IPv6 literal keeps its colons inside brackets.
    """
    userinfo_end = value.rfind("@", scheme_end, authority_end)
    host_start = scheme_end if userinfo_end < 0 else userinfo_end + 1
    if value[host_start : host_start + 1] == "[":
        bracket_end = value.find("]", host_start, authority_end)
        if bracket_end < 0:
            return -1
        host_start = bracket_end + 1
    return value.find(":", host_start, authority_end)


def split_operand(value: str) -> typing.Optional[typing.Tuple[str, str]]:
    """Split ``URI:PATH`` into its URI and path; ``None`` for a local path."""
    if not _URI_OPERAND.match(value) or re.match(r"^[A-Za-z]:[\\/]", value):
        return None
    if "://" in value:
        scheme_end = value.find("://") + 3
        authority_end = value.find("/", scheme_end)
        search_end = len(value) if authority_end < 0 else authority_end
        # The userinfo may hold colons (`user:password@host`), so the path
        # separator is only looked for after it.
        userinfo_end = value.rfind("@", scheme_end, search_end)
        host_start = scheme_end if userinfo_end < 0 else userinfo_end + 1
        shown = redact_uri(value)
        separator = value.rfind(":", host_start, search_end)
        if separator < host_start:
            raise ValueError(
                f"remote path operand must be URI:PATH: {shown!r} has no "
                "path separator"
            )
        if (
            separator == _port_colon(value, scheme_end, search_end)
            and value[separator + 1 : search_end].isdigit()
        ):
            # `ssh://host:2222/x` has no path colon, so the last colon before
            # the authority's slash is the port's; splitting there would reach
            # `ssh://host` and the relative path `2222/x`.
            raise ValueError(
                f"remote path operand must be URI:PATH: {shown!r} ends at a "
                "port, not a path (write it as ssh://host:2222:/x)"
            )
        uri, path = value[:separator], value[separator + 1 :]
    else:
        # An opaque URI has no authority, so the scheme's own colon ends it:
        # `local:/tmp/x`. The scheme keeps its colon -- `local:` is the URI.
        scheme_end = value.find(":")
        uri, path = value[: scheme_end + 1], value[scheme_end + 1 :]
    if not path:
        raise ValueError(
            f"remote path operand requires a path: {redact_uri(value)!r} names a host "
            "and no file (write it as URI:PATH, e.g. local:/tmp/x or "
            "ssh://host:22:/srv/app)"
        )
    return uri, path


def _uri_has_password(uri: str) -> bool:
    try:
        return urlsplit(uri).password is not None
    except ValueError:
        return False


def _on_a_terminal() -> bool:
    """Whether a prompt can be answered: a terminal on stdin, not a tool call."""
    if running_mode() == "mcp":
        return False
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


class PasswordPrompt:
    """A password asked for on the terminal the first time a host needs one.

    The answer is kept, so every host of a run and every connection the
    transport opens get the same one, and two threads never prompt at once.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._asked = False
        self._answer: typing.Optional[str] = None

    def __call__(self) -> typing.Optional[str]:
        with self._lock:
            if not self._asked:
                self._answer = getpass.getpass("Password: ")
                self._asked = True
            return self._answer


def no_password() -> typing.Optional[str]:
    """What a host that needs a password gets where nothing can prompt."""
    raise PermissionError(
        "a password is needed and there is no terminal to ask on; "
        "pass -p PASSWORD or set HOSTCTL_PASSWORD"
    )


def _refused_login(error: OperationNotStartedError) -> typing.Optional[PermissionError]:
    """The `PermissionError` that made every provider decline, if that is what
    did: a refused login is never sent anything, so it arrives as a decline."""
    cause: typing.Optional[BaseException] = error
    while isinstance(cause, OperationNotStartedError):
        cause = cause.cause
    return cause if isinstance(cause, PermissionError) else None


@contextlib.contextmanager
def _reporting_refusals(host: Host) -> typing.Iterator[Host]:
    try:
        with host:
            yield host
    except OperationNotStartedError as error:
        refused = _refused_login(error)
        if refused is None:
            raise
        raise PermissionError(str(refused)) from error


class Credentialed(LoggingArgs, Cmd):
    """A command that may connect to a host and so may need a password."""

    _logger_name_ = "hostctl"

    username: typing.Optional[str] = None
    "Log in as USER, in place of the URI's user"
    ("-u", "--username")

    password: Arg[typing.Optional[str], Meta(env="HOSTCTL_PASSWORD")] = None
    "Log in with PASSWORD, in place of the URI's or HOSTCTL_PASSWORD ('' for none)"
    ("-p", "--password")

    # A tool caller may name a local file: a ProxyCommand in it starts a
    # local process, which `run local:` already lets that caller do.
    ssh_config: Arg[typing.Optional[str], Meta(metavar="PATH")] = None
    "Read PATH as the SSH config, in place of ~/.ssh/config ('' for none)"
    ("--ssh-config",)

    def credentials(self) -> dict:
        """`-u`, and the password from `-p` or else `HOSTCTL_PASSWORD`.

        Without either, a host that asks for a password gets a prompt on a
        terminal, read once for every host, and an error everywhere else.
        """
        found: typing.Dict[str, object] = {}
        if self.username is not None:
            found["username"] = self.username
        if self.password is None:
            found[_ASK] = PasswordPrompt() if _on_a_terminal() else no_password
        else:
            found["password"] = self.password
            if value_sources(self).get("password") == "env":
                found[_AMBIENT] = True
        return found

    def open(self, uri: str, credentials: dict) -> typing.ContextManager[Host]:
        """The host `uri` names, offered what of `credentials` it accepts.

        A login every provider refused is raised as that `PermissionError`.
        """
        if self.ssh_config is None:
            return _reporting_refusals(Host(uri, **offered(uri, credentials)))
        config = with_ssh_config(
            HostConfig(uri, **offered(uri, credentials)), self.ssh_config
        )
        return _reporting_refusals(config._create_host())

    def open_host(self, stack: contextlib.ExitStack, uri: str) -> Host:
        """Connect to `uri` for the life of `stack`."""
        return stack.enter_context(self.open(uri, self.credentials()))

    def path_operand(self, stack: contextlib.ExitStack, value: str) -> Path:
        """Resolve ``URI:PATH`` on its host, or an ordinary local path."""
        split = split_operand(value)
        if split is None:
            return Path(value)
        uri, path = split
        return self.open_host(stack, uri).path(path)


class HostCommand(Credentialed):
    """A command that acts on the one host its URI names."""

    uri: str
    "Connection URI (see `hostctl --help`)"
    ("uri",)

    def connect(self) -> typing.ContextManager[Host]:
        """The host, as a context manager that closes it."""
        return self.open(self.uri, self.credentials())
