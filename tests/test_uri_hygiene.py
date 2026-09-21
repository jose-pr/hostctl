"""What a connection URI may carry, and what a diagnostic may do about it.

These are the parsing edges a real inventory produces: a host with a stray
control byte, a name whose lowercase is longer than it is, a typo'd port in a
string someone is trying to log.
"""

from __future__ import annotations

import pytest

from hostctl import ConnectionString, HostConfig, redact_uri


@pytest.mark.parametrize("code", ("\x00", "\x1b", "\x0b", "\x7f"))
def test_every_control_character_is_refused_in_a_host(code):
    """Only TAB, CR and LF were checked -- the three `urlsplit` deletes -- so
    NUL, ESC, VT and DEL travelled into `config.host` and from there into
    every log line built from it. An ESC sequence there is terminal-escape
    injection into whatever reads the log."""
    with pytest.raises(ValueError, match="control character"):
        HostConfig(f"ssh://nas{code}x/")


def test_a_percent_encoded_control_character_is_refused_too():
    with pytest.raises(ValueError, match="control character"):
        HostConfig("ssh://nas%1bx/")


def test_a_host_whose_lowercase_is_longer_keeps_its_own_text():
    """`urlsplit().hostname` is case-folded, and the original was recovered by
    slicing `len(hostname)` -- but lowercasing is not length-preserving
    (U+0130 folds to two characters), so the slice ran into the port's colon.
    The stored host gained a trailing ':' and was rendered as a bracketed
    IPv6 literal: unresolvable, and malformed as a URI."""
    config = HostConfig("ssh://user@İzmir-nas.example:2222")

    assert config.host == "İzmir-nas.example"
    assert "[" not in config.connection_uri
    assert ":2222" in config.connection_uri


@pytest.mark.parametrize(
    "uri",
    (
        "ssh://u:hunter2@host:notaport",
        "ssh://u:hunter2@host:99999",
        "ssh://u:hunter2@[::1",
    ),
)
def test_redact_uri_never_raises_and_still_removes_the_password(uri):
    """It is documented as never raising because it is called from inside
    exception handlers: raising replaced the real error with a ValueError
    from the logging call."""
    redacted = redact_uri(uri)

    assert "hunter2" not in redacted


def test_a_well_formed_uri_still_round_trips_through_redaction():
    assert redact_uri("ssh://user:hunter2@nas:2222/") == "ssh://user@nas:2222/"


def test_an_ipv6_literal_host_survives_parsing():
    config = ConnectionString("ssh://[2001:db8::1]:2222")

    assert config.host == "2001:db8::1"
    assert config.port == 2222
