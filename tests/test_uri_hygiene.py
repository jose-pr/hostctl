"""What a connection URI may carry, and what a diagnostic may do about it.

These are the parsing edges a real inventory produces: a host with a stray
control byte, a name whose lowercase is longer than it is, a typo'd port in a
string someone is trying to log.
"""

from __future__ import annotations

import pytest

from hostctl.uri._connection import ConnectionString
from hostctl.transports.container._host import ContainerConfig
from hostctl.host._contracts import HostConfig
from hostctl.system._system import PosixConfig
from hostctl.transports.ssh._transport import SSHConfig
from hostctl.transports.winrm._transport import WinRMConfig
from hostctl.uri._parse import redact_uri


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


@pytest.mark.parametrize(
    "uri",
    (
        "posix://%C2%9Bnode",
        "posix://%1B@node",
        "posix://%1Bnode",
        "ssh://%C2%9Bnas",
    ),
)
def test_an_encoded_c0_or_c1_control_character_is_refused(uri):
    """`%C2%9B` is the C1 control CSI. It passed the encoded-C0 check, and a
    system URI unquotes the whole netloc, so it reached `authority` and from
    there `info().hostname`."""
    with pytest.raises(ValueError, match="control character"):
        HostConfig(uri)


@pytest.mark.parametrize(
    "build",
    (
        lambda uri: SSHConfig(uri),
        lambda uri: WinRMConfig(uri, "u"),
        lambda uri: ContainerConfig(uri),
        lambda uri: PosixConfig(uri),
    ),
    ids=("ssh", "winrm", "container", "system"),
)
def test_a_config_refuses_a_uri_as_its_host_without_echoing_the_password(build):
    with pytest.raises(ValueError, match="takes a host, not a URI") as caught:
        build("winrm://u:dummyPW@h")

    assert "dummyPW" not in str(caught.value)


_CUT_SHORT = (
    ("ssh://user:Pw9?cret@host", ("Pw9", "cret")),
    ("winrm://u:Pw9?cret@h", ("Pw9", "cret")),
    ("ssh://user:123?a=b@host", ("123", "a=b")),
    ("ssh://user:Pw9/cret@host", ("Pw9", "cret")),
    ("qga+ssh://user:Pw9/cret@host?domain=d", ("Pw9", "cret")),
    ("ssh://user:Pw9#cret@host", ("Pw9", "cret")),
)


@pytest.mark.parametrize("uri, fragments", _CUT_SHORT)
def test_a_password_cut_short_is_never_echoed(uri, fragments):
    """A generated password holding `/`, `?` or `#` ends the authority early;
    the remainder must not be echoed as a "query key" or a "port"."""
    with pytest.raises(ValueError) as caught:
        HostConfig(uri)
    shown = " ".join(
        repr(item)
        for item in (caught.value, caught.value.__cause__, caught.value.__context__)
    )
    assert "percent-encode" in str(caught.value)
    for fragment in fragments:
        assert fragment not in shown
    with pytest.raises(ValueError, match="percent-encode"):
        HostConfig.supported_credentials(uri)


@pytest.mark.parametrize(
    "uri", ("ssh://user:Pw9?cret@host", "ssh://user:Pw9/cret@host")
)
def test_a_connection_string_with_a_cut_short_password_does_not_echo_it(uri):
    with pytest.raises(ValueError) as caught:
        ConnectionString(uri)
    shown = " ".join(
        repr(item)
        for item in (caught.value, caught.value.__cause__, caught.value.__context__)
    )
    assert "Pw9" not in shown
    assert "cret" not in shown


@pytest.mark.parametrize(
    "uri", ("qga+unix:///run/q@v.sock?domain=d", "serial:///dev/a@b")
)
def test_an_at_sign_after_an_empty_authority_still_builds(uri):
    assert HostConfig(uri)


def test_a_real_parameter_name_is_still_reported():
    with pytest.raises(ValueError, match="unknown connection parameter: bogus"):
        HostConfig("ssh://h?bogus=1")
