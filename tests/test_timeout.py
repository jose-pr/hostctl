"""`Timeout` and `DEFAULT`: the one value every config spells its timeouts with."""

from __future__ import annotations

import copy
import dataclasses
import pickle

import pytest

import hostctl
from hostctl import DEFAULT, Timeout
from hostctl.host import _timeout


def test_both_names_are_exported_from_the_root_and_their_home():
    import hostctl.host as home

    assert {"Timeout", "DEFAULT"} <= set(hostctl.__all__)
    assert {"Timeout", "DEFAULT"} <= set(home.__all__)
    assert home.Timeout is Timeout and home.DEFAULT is DEFAULT


def test_every_field_defaults_to_default():
    timeout = Timeout()

    assert (timeout.total, timeout.connect, timeout.read, timeout.write) == (
        DEFAULT,
        DEFAULT,
        DEFAULT,
        DEFAULT,
    )


def test_a_positional_number_is_the_total():
    assert Timeout(30) == Timeout(total=30)
    assert Timeout(30).connect is DEFAULT


def test_fields_after_total_are_keyword_only():
    with pytest.raises(TypeError):
        Timeout(30, 10)  # type: ignore[misc]


def test_numbers_are_stored_as_float_and_equal_spellings_are_equal():
    assert Timeout(30).total == 30.0 and isinstance(Timeout(30).total, float)
    assert Timeout(30, read=1) == Timeout(30.0, read=1.0)
    assert hash(Timeout(30, read=1)) == hash(Timeout(30.0, read=1.0))
    assert Timeout(30) != Timeout(31)
    assert Timeout(None) != Timeout()


def test_none_means_no_limit_and_is_kept():
    assert Timeout(None, connect=None).connect is None


@pytest.mark.parametrize("value", [0, -1, -0.5, float("inf"), float("nan")])
def test_a_non_positive_or_infinite_value_raises_value_error(value):
    with pytest.raises(ValueError, match="timeout.read must be a positive"):
        Timeout(read=value)


@pytest.mark.parametrize("value", [True, "30", b"30", object()])
def test_a_value_that_is_not_seconds_raises_type_error(value):
    with pytest.raises(TypeError, match="timeout.write must be a number"):
        Timeout(write=value)


def test_the_repr_shows_only_fields_that_are_not_default():
    assert repr(Timeout()) == "Timeout()"
    assert repr(Timeout(30)) == "Timeout(total=30.0)"
    assert repr(Timeout(connect=5, write=None)) == "Timeout(connect=5.0, write=None)"
    assert repr(DEFAULT) == "DEFAULT" == str(DEFAULT)


@pytest.mark.parametrize(
    "timeout", [Timeout(), Timeout(30), Timeout(None, connect=2.5, write=None)]
)
def test_the_repr_evaluates_back_to_an_equal_value(timeout):
    assert eval(repr(timeout), {"Timeout": Timeout, "DEFAULT": DEFAULT}) == timeout


def test_it_is_immutable():
    timeout = Timeout(30)

    with pytest.raises(dataclasses.FrozenInstanceError):
        timeout.total = 5  # type: ignore[misc]


def test_dataclasses_replace_validates_the_new_value():
    timeout = Timeout(30)

    assert dataclasses.replace(timeout, connect=5) == Timeout(30, connect=5)
    with pytest.raises(ValueError):
        dataclasses.replace(timeout, read=0)


def test_it_and_default_pickle_and_copy():
    timeout = Timeout(30, read=None)

    assert pickle.loads(pickle.dumps(timeout)) == timeout
    assert copy.deepcopy(timeout) == timeout
    assert pickle.loads(pickle.dumps(DEFAULT)) is DEFAULT
    assert copy.copy(DEFAULT) is DEFAULT


def test_comparison_with_another_type_is_not_equal_and_does_not_raise():
    assert Timeout(30) != 30
    assert Timeout() != DEFAULT


@pytest.mark.parametrize(
    "value,expected",
    [
        (60, Timeout(total=60)),
        (2.5, Timeout(total=2.5)),
        (None, Timeout(total=None)),
        (DEFAULT, Timeout()),
    ],
)
def test_coerce_turns_a_number_or_none_into_a_total(value, expected):
    assert Timeout.coerce(value) == expected


def test_coerce_returns_a_timeout_unchanged():
    timeout = Timeout(connect=3)

    assert Timeout.coerce(timeout) is timeout


@pytest.mark.parametrize("value", ["30", True, [30]])
def test_coerce_refuses_anything_else(value):
    with pytest.raises(TypeError, match="timeout must be"):
        Timeout.coerce(value)


class TestURIKeys:
    def test_absent_keys_are_default(self):
        assert _timeout._timeout_from_query({}) == Timeout()

    def test_an_empty_value_is_none(self):
        assert _timeout._timeout_from_query({"timeout_read": ""}) == Timeout(read=None)

    def test_each_field_has_its_key(self):
        query = {
            "timeout": "30",
            "timeout_connect": "5",
            "timeout_read": "0.5",
            "timeout_write": "",
        }

        assert _timeout._timeout_from_query(query) == Timeout(
            30, connect=5, read=0.5, write=None
        )

    @pytest.mark.parametrize("text", ["soon", "0", "-1"])
    def test_a_bad_value_names_the_key(self, text):
        with pytest.raises(ValueError, match="timeout_connect"):
            _timeout._timeout_from_query({"timeout_connect": text})

    def test_only_fields_that_are_not_default_are_rendered(self):
        assert _timeout._timeout_query(Timeout()) == []
        assert _timeout._timeout_query(Timeout(30, write=None)) == [
            ("timeout", "30"),
            ("timeout_write", ""),
        ]
        assert _timeout._timeout_query(Timeout(read=0.25)) == [("timeout_read", "0.25")]
