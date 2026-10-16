"""What every command shares: credentials, the host operand, and output."""

from __future__ import annotations

import contextlib
import getpass
import io
import os
import re
import typing

from duho import Cmd, CommandError, LoggingArgs, running_mode

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


class Credentialed(LoggingArgs, Cmd):
    """A command that may connect to a host and so may need a password."""

    _logger_name_ = "hostctl"

    ask_password: bool = False
    "Prompt for a password instead of reading HOSTCTL_PASSWORD"

    def credentials(self) -> dict:
        """The password from `--ask-password` or `HOSTCTL_PASSWORD`, if any."""
        password = os.environ.get("HOSTCTL_PASSWORD")
        if self.ask_password:
            if running_mode() == "mcp":
                # A tool call has no terminal; the prompt would wait forever.
                raise CommandError(
                    "--ask-password cannot prompt in a tool call; set "
                    "HOSTCTL_PASSWORD in the server's environment",
                    FAILED,
                )
            password = getpass.getpass("Password: ")
        return {"password": password} if password is not None else {}

    def open_host(self, stack: contextlib.ExitStack, uri: str) -> Host:
        """Connect to `uri` for the life of `stack`."""
        credentials = usable_credentials(uri, self.credentials())
        return stack.enter_context(Host(uri, **credentials))

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

    def connect(self) -> Host:
        """The host, as a context manager that closes it."""
        return self.open(self.uri, self.credentials())

    def open(self, uri: str, credentials: dict) -> Host:
        """The host `uri` names, offered what of `credentials` it accepts."""
        return Host(uri, **usable_credentials(uri, credentials))
