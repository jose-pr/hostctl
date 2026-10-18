"""Where an SSH host's OpenSSH client configuration comes from.

asyncssh reads a client configuration only from files (`SSHConfig.parse`
opens a path), so configuration text reaches it through a private temporary
file that exists only while a connection is being opened.
"""

from __future__ import annotations

import atexit
import contextlib
import locale
import os
import tempfile
import threading
import typing


class _UserSSHConfig:
    """The type of `USER_SSH_CONFIG`; it has one instance."""

    __slots__ = ()
    _instance: typing.ClassVar[typing.Optional[_UserSSHConfig]] = None

    def __new__(cls) -> _UserSSHConfig:
        if cls._instance is None:
            cls._instance = object.__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "USER_SSH_CONFIG"

    def __reduce__(self) -> str:
        # Pickle and copy by name, so the copy is the same object.
        return "USER_SSH_CONFIG"


#: The user's own OpenSSH client configuration, `~/.ssh/config`, as `ssh`
#: reads it: `~` is resolved each time a connection opens, and a missing file
#: reads nothing.
USER_SSH_CONFIG = _UserSSHConfig()


class _SSHConfigText:
    """Configuration text from the caller, kept out of `repr()`.

    An object with `read_text`/`open` (a path, possibly on another host) is
    read when the first connection opens, not here, and the text is kept: a
    constructor does no I/O, and a config does not change once it connected.
    """

    __slots__ = ("_text", "_source", "_lock", "_reading")

    def __init__(
        self, text: typing.Optional[str] = None, source: typing.Any = None
    ) -> None:
        self._text = text
        self._source = source
        self._lock = threading.RLock()
        self._reading = False

    def text(self) -> str:
        """The configuration text, read from the source the first time."""
        with self._lock:
            if self._text is None:
                if self._reading:
                    raise RuntimeError(
                        "ssh_config is read through the connection it configures"
                    )
                self._reading = True
                try:
                    text = _read_source(self._source)
                finally:
                    self._reading = False
                self._text = _checked(text)
            return self._text

    def __repr__(self) -> str:
        return "<ssh_config text>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _SSHConfigText):
            return NotImplemented
        if self is other:
            return True
        if self._source is not None or other._source is not None:
            return False
        return self._text == other._text

    __hash__ = None  # type: ignore[assignment]

    def __reduce__(self) -> typing.Tuple[typing.Any, ...]:
        return (_SSHConfigText, (self._text, self._source))


def _ssh_config_source(value: typing.Any) -> typing.Any:
    """`value` as `SSHConfig.ssh_config` keeps it.

    One of `USER_SSH_CONFIG`, `None`, an `os.PathLike` or `_SSHConfigText`,
    so `dataclasses.replace()` passes it back unchanged and nothing is read
    twice.
    """
    if value is USER_SSH_CONFIG or isinstance(value, _SSHConfigText):
        return value
    if value is None or value is False:
        return None
    if value is True:
        raise ValueError(
            "ssh_config=True names no configuration; the default, "
            "USER_SSH_CONFIG, reads ~/.ssh/config"
        )
    if isinstance(value, str):
        return _SSHConfigText(_checked(value))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _SSHConfigText(_checked(_decoded(bytes(value))))
    if isinstance(value, os.PathLike):
        return value
    if callable(getattr(value, "read_text", None)) or callable(
        getattr(value, "open", None)
    ):
        return _SSHConfigText(source=value)
    if callable(getattr(value, "read", None)):
        data = value.read()
        text = data if isinstance(data, str) else _decoded(bytes(data))
        return _SSHConfigText(_checked(text))
    raise TypeError(
        "ssh_config must be USER_SSH_CONFIG, None, an os.PathLike file, an "
        "object with read_text() or open(), a readable stream, or the "
        f"configuration text as str or bytes, not {type(value).__name__}"
    )


def _read_source(source: typing.Any) -> str:
    read_text = getattr(source, "read_text", None)
    if callable(read_text):
        return read_text(encoding="utf-8")
    with source.open("rb") as stream:
        data = stream.read()
    return data if isinstance(data, str) else _decoded(bytes(data))


def _decoded(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("ssh_config bytes are read as UTF-8, and are not") from exc


def _checked(text: str) -> str:
    """`text`, refused where no OpenSSH configuration could look like it.

    asyncssh skips a keyword it does not know, so a file name passed as the
    text would otherwise connect with no configuration and say nothing.
    """
    for number, line in enumerate(text.splitlines(), 1):
        words = line.split()
        if len(words) == 1 and not words[0].startswith("#") and "=" not in words[0]:
            raise ValueError(
                f"ssh_config line {number} is a keyword with no value: a str "
                "is the configuration's text; name a file as pathlib.Path(...)"
            )
    _encoded(text)
    return text


def _encoded(text: str) -> bytes:
    # asyncssh opens the file in text mode with no encoding, so it is written
    # in the one `open()` reads with, and the text comes back unchanged.
    encoding = locale.getpreferredencoding(False)
    try:
        return text.encode(encoding)
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"ssh_config holds characters {encoding!r} cannot encode, and "
            "asyncssh reads its configuration in that encoding"
        ) from exc


def _user_ssh_config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".ssh", "config")


def _asyncssh_config(source: typing.Any) -> typing.Any:
    """`source` as asyncssh's `config=`, with `USER_SSH_CONFIG` resolved now.

    Configuration text stays a `_SSHConfigText`; `_materialized()` turns it
    into a file for the length of one connect.
    """
    if source is None or isinstance(source, _SSHConfigText):
        return source
    if source is USER_SSH_CONFIG:
        path = _user_ssh_config_path()
        if os.path.isfile(path) and os.access(path, os.R_OK):
            return [path]
        return None
    return [os.path.expanduser(os.fsdecode(source))]


_LIVE: typing.Set[str] = set()
_LIVE_LOCK = threading.Lock()


@contextlib.contextmanager
def _materialized(
    options: typing.Dict[str, object],
) -> typing.Iterator[typing.Dict[str, object]]:
    """`options`, with configuration text written to a file while in use."""
    source = options.get("config")
    if not isinstance(source, _SSHConfigText):
        yield options
        return
    data = _encoded(source.text())
    # mkstemp creates the file readable and writable by this user only.
    descriptor, name = tempfile.mkstemp(prefix="hostctl-ssh-config-", suffix=".conf")
    with _LIVE_LOCK:
        _LIVE.add(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        yield {**options, "config": [name]}
    finally:
        _remove(name)


def _remove(name: str) -> None:
    try:
        os.unlink(name)
    except FileNotFoundError:
        pass
    except OSError:
        # Still open elsewhere on Windows; the exit handler tries again.
        return
    with _LIVE_LOCK:
        _LIVE.discard(name)


@atexit.register
def _remove_leftovers() -> None:
    with _LIVE_LOCK:
        names = list(_LIVE)
    for name in names:
        _remove(name)
