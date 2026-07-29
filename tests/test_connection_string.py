"""`ConnectionString` parses what a user types, and never renders a password."""

from __future__ import annotations

import pytest

from hostctl import ConnectionString


def test_a_bare_host_parses_with_a_supplied_scheme():
    # A bare host is not an invalid URI -- it is a URI with the scheme left
    # off, which is what people type. Requiring a second parser for it is
    # what this replaces.
    target = ConnectionString("nas", scheme="wss")

    assert target.scheme == "wss"
    assert target.host == "nas"
    assert str(target) == "wss://nas"


def test_a_bare_host_and_port_parses():
    # `nas:8443` reads as the scheme `nas` to urlsplit, not as a host.
    target = ConnectionString("nas:8443", scheme="wss")

    assert (target.scheme, target.host, target.port) == ("wss", "nas", 8443)


def test_a_missing_scheme_with_nothing_to_supply_one_is_an_error():
    # Neither `scheme=` nor `defaults=` given, and the string has none.
    with pytest.raises(ValueError, match="no scheme"):
        ConnectionString("nas")


def test_scheme_is_an_override_not_a_default():
    # `scheme=` is the same kind of argument as every other field: it wins
    # over the string. Supplying one only when the string omits it is what
    # `defaults=` is for.
    assert ConnectionString("wss://nas", scheme="ssh").scheme == "ssh"
    assert ConnectionString("wss://nas", defaults={"scheme": "ssh"}).scheme == "wss"
    assert ConnectionString("nas", defaults={"scheme": "ssh"}).scheme == "ssh"


def test_default_ports_fill_only_a_missing_port():
    ports = {"wss": 443, "ws": 80}

    assert ConnectionString("nas", scheme="wss", default_ports=ports).port == 443
    # A port in the string wins over the table.
    assert ConnectionString("nas:9", scheme="wss", default_ports=ports).port == 9
    # ...as does an explicit `port=`.
    assert ConnectionString("nas", scheme="wss", port=9, default_ports=ports).port == 9
    # No table, no invention.
    assert ConnectionString("nas", scheme="wss").port is None


def test_the_host_keeps_the_spelling_it_was_given():
    # urlsplit case-folds `hostname`, which is right for resolution and wrong
    # for text rendered back to a caller.
    target = ConnectionString("wss://root:pw@nasA:8443/api")

    assert target.host == "nasA"
    assert "nasA" in str(target)


def test_credentials_are_parsed_but_never_rendered():
    target = ConnectionString("wss://root:hunter2@nas:8443/api")

    assert target.username == "root"
    assert target.password == "hunter2"
    # Removed, not masked: the rendered form stays valid and reusable, so it
    # can never round-trip a wrong credential.
    assert str(target) == "wss://root@nas:8443/api"
    assert "hunter2" not in str(target)
    assert "hunter2" not in repr(target)
    assert "*" not in str(target)


def test_the_password_is_absent_from_repr_so_a_traceback_cannot_leak_it():
    # A value reaching a log line or a traceback frame renders through repr.
    assert "hunter2" not in repr(ConnectionString("wss://root:hunter2@nas"))


def test_a_password_carries_credential_extras_after_a_newline():
    # Written raw: characters urlsplit would silently delete are encoded
    # before parsing.
    target = ConnectionString("wss://root:hunter2\notp:123456@nas")

    assert target.password == "hunter2"
    assert dict(target.extras) == {"otp": "123456"}
    assert "123456" not in str(target)


def test_a_control_character_in_the_host_is_rejected():
    # Deletion there rewrites the target rather than losing data.
    with pytest.raises(ValueError):
        ConnectionString("wss://na\ns.other.example/")


def test_is_local_is_a_string_check_with_no_lookup(monkeypatch):
    import socket

    def _fail(*args, **kwargs):
        raise AssertionError("is_local must not resolve anything")

    monkeypatch.setattr(socket, "gethostbyname", _fail)
    monkeypatch.setattr(socket, "getaddrinfo", _fail)

    assert ConnectionString("localhost", scheme="wss").is_local
    assert ConnectionString("127.0.0.1", scheme="wss").is_local
    assert ConnectionString("wss://LocalHost").is_local
    assert not ConnectionString("nas", scheme="wss").is_local
    # An unresolvable name answers instead of raising.
    assert not ConnectionString("no-such-host.invalid", scheme="wss").is_local


def test_query_helpers_keep_order_and_repeats():
    target = ConnectionString("wss://nas/api?a=1&b=2&a=3")

    assert target.qsl == [("a", "1"), ("b", "2"), ("a", "3")]
    # Last wins, as a URI reader would take it.
    assert target.query_val("a") == "3"
    assert target.query_val("missing", "fallback") == "fallback"


def test_ipv6_literals_round_trip_with_their_brackets():
    target = ConnectionString("ssh://u:p@[2001:DB8::1]:22")

    assert target.host == "2001:DB8::1"
    assert str(target) == "ssh://u@[2001:DB8::1]:22"


def test_path_query_and_fragment_are_preserved():
    target = ConnectionString("wss://nas:8443/api/v2?x=1#section")

    assert target.path == "/api/v2"
    assert target.query == "x=1"
    assert target.fragment == "section"
    assert str(target) == "wss://nas:8443/api/v2?x=1#section"


def test_wrapping_an_existing_value_keeps_every_field():
    original = ConnectionString("wss://root:hunter2\notp:1@nasA:8443/api")
    copied = ConnectionString(original)

    assert copied == original
    # Including the credentials, which `str()` deliberately omits.
    assert copied.password == "hunter2"
    assert dict(copied.extras) == {"otp": "1"}


def test_every_field_can_be_supplied_directly():
    target = ConnectionString(
        "nas",
        scheme="ssh",
        port=2222,
        username="root",
        password="hunter2",
        path="/srv",
        query="a=1",
        fragment="f",
    )

    assert str(target) == "ssh://root@nas:2222/srv?a=1#f"
    assert target.password == "hunter2"


def test_an_explicit_argument_wins_over_the_string():
    # The caller wrote it in the call, so it is the more specific statement.
    assert ConnectionString("wss://nas:8443", port=1).port == 1
    assert ConnectionString("wss://a", host="b").host == "b"
    assert ConnectionString("wss://u@nas", username="other").username == "other"


def test_defaults_fill_gaps_but_never_override():
    # A default is what to use when nothing else said; it must not win over
    # something the caller actually wrote in the string.
    assert ConnectionString("wss://nas:8443", defaults={"port": 443}).port == 8443
    assert ConnectionString("wss://nas", defaults={"port": 443}).port == 443
    # ...nor over an explicit argument.
    assert ConnectionString("wss://nas", port=1, defaults={"port": 443}).port == 1


def test_defaults_accept_a_connection_string_as_a_profile():
    profile = ConnectionString("wss://root@nas", port=443)

    assert str(ConnectionString("other", defaults=profile)) == "wss://root@other:443"
    # Only fields the profile carries count, so its empty path does not
    # override a path the string supplies.
    assert ConnectionString("wss://h/api", defaults=profile).path == "/api"


def test_a_scheme_from_defaults_also_lets_a_bare_host_parse():
    assert str(ConnectionString("nas", defaults={"scheme": "wss"})) == "wss://nas"


def test_an_unknown_default_field_is_rejected():
    with pytest.raises(TypeError, match="unknown default field"):
        ConnectionString("nas", scheme="wss", defaults={"hostname": "typo"})


def test_replace_returns_a_changed_copy():
    target = ConnectionString("wss://nas:8443/api")
    moved = target.replace(host="other", port=443)

    assert str(moved) == "wss://other:443/api"
    assert str(target) == "wss://nas:8443/api"
