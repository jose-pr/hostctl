"""`verify`: the one certificate and host-key switch every network config spells.

`True` checks with the transport's own trust, `False` switches every check
off, and a string (TLS transports only) names the CA bundle -- a PEM file or
an OpenSSL hashed directory -- trusted instead of that default.
"""

from __future__ import annotations

import os
import typing

from ..uri._parse import _FALSE, _TRUE

#: What a TLS transport's `verify` holds once validated.
_TLSVerify = typing.Union[bool, str]


def _bool_spelling(text: str) -> typing.Optional[bool]:
    folded = text.casefold()
    if folded in _TRUE:
        return True
    if folded in _FALSE:
        return False
    return None


def _tls_verify(owner: str, value: object) -> _TLSVerify:
    """`value` validated as a TLS `verify`: a bool, or a CA bundle path as `str`.

    A string spelling a boolean (`"false"`, `"0"`) is refused: read as a path
    it would fail only at connect time, far from the mistake.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str):
        raise TypeError(
            f"{owner}: verify must be True, False, or a CA bundle path, "
            f"not {type(value).__name__}"
        )
    if not value:
        raise ValueError(f"{owner}: verify must not be an empty path")
    if _bool_spelling(value) is not None:
        raise ValueError(
            f"{owner}: verify={value!r} is a string; pass the bool "
            f"{_bool_spelling(value)}, or a path such as './{value}'"
        )
    return value


def _host_key_verify(owner: str, value: object) -> bool:
    """`value` validated as an SSH `verify`, which has no CA bundle."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, os.PathLike)):
        raise TypeError(
            f"{owner}: verify is True or False; SSH has no CA bundle -- "
            "name a known_hosts file with known_hosts="
        )
    raise TypeError(f"{owner}: verify must be True or False")


def _verify_from_query(query: typing.Mapping[str, str], *, bundle: bool) -> _TLSVerify:
    """`verify=` from a parsed URI query: a boolean spelling, else a bundle path."""
    if "verify" not in query:
        return True
    value = str(query["verify"])
    spelled = _bool_spelling(value)
    if spelled is not None:
        return spelled
    if not bundle:
        raise ValueError("verify must be one of 1/true/yes/on or 0/false/no/off")
    if not value:
        raise ValueError("verify must not be empty")
    return value


def _verify_query_text(value: _TLSVerify) -> str:
    """The URI query value of `verify`; a path is returned as is, to be encoded."""
    if isinstance(value, bool):
        return str(value).lower()
    return value


def _ca_bundle(path: str) -> str:
    """`path` when it names a file or directory; raises `FileNotFoundError` otherwise.

    Checked before connecting, so the error names the path rather than
    surfacing as a TLS failure from inside the HTTP library.
    """
    if not (os.path.isfile(path) or os.path.isdir(path)):
        raise FileNotFoundError(f"CA bundle not found: {path}")
    return path
