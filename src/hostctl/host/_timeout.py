"""`Timeout`: the one way every config spells its timeouts."""

from __future__ import annotations

import dataclasses
import enum
import math
import typing
from urllib.parse import urlencode


class _Default(enum.Enum):
    """The type of :data:`DEFAULT`; its one member is compared by identity."""

    DEFAULT = "DEFAULT"

    def __repr__(self) -> str:
        return "DEFAULT"

    __str__ = __repr__


#: A `Timeout` field left to the transport: its own documented default.
DEFAULT = _Default.DEFAULT

#: What a `Timeout` field holds: seconds, `None` for no limit, or `DEFAULT`.
_Seconds = typing.Union[float, None, _Default]

_FIELDS = ("total", "connect", "read", "write")


def _seconds(name: str, value: object) -> _Seconds:
    """`value` validated as one `Timeout` field."""
    if value is DEFAULT or value is None:
        return value  # type: ignore[return-value]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"timeout.{name} must be a number of seconds, None or DEFAULT, "
            f"not {type(value).__name__}"
        )
    if not value > 0 or math.isinf(value):
        raise ValueError(
            f"timeout.{name} must be a positive, finite number of seconds "
            "(None for no limit, DEFAULT for the transport's own)"
        )
    return float(value)


@dataclasses.dataclass(frozen=True, init=False, repr=False)
class Timeout:
    """How long a host's operations may take, field by field, in seconds.

    `total` bounds a whole command and is the default of ``run(timeout=)``;
    `connect` bounds establishing a connection, session or port; `read` one
    reply or one read; `write` one write. Each field is `DEFAULT` (the
    transport's own), `None` (no limit) or a positive number of seconds.
    ``Timeout(30)`` sets `total` only. A transport that cannot honour a field
    refuses any value but `DEFAULT` for it when the config is built.

    Raises `TypeError` for a value that is not a number, `None` or `DEFAULT`,
    and `ValueError` for zero, a negative number or infinity.
    """

    total: _Seconds = DEFAULT
    connect: _Seconds = DEFAULT
    read: _Seconds = DEFAULT
    write: _Seconds = DEFAULT

    def __init__(
        self,
        total: _Seconds = DEFAULT,
        *,
        connect: _Seconds = DEFAULT,
        read: _Seconds = DEFAULT,
        write: _Seconds = DEFAULT,
    ) -> None:
        for name, value in zip(_FIELDS, (total, connect, read, write)):
            object.__setattr__(self, name, _seconds(name, value))

    @classmethod
    def coerce(cls, value: typing.Union[float, None, Timeout]) -> Timeout:
        """A `Timeout` for what a config's ``timeout=`` accepts.

        A `Timeout` is returned as it is; a number or `None` sets `total`
        only (``coerce(60) == Timeout(total=60)``); `DEFAULT` is
        ``Timeout()``. Anything else raises `TypeError`.
        """
        if isinstance(value, Timeout):
            return value
        if value is DEFAULT:
            return cls()
        if value is None or (
            isinstance(value, (int, float)) and not isinstance(value, bool)
        ):
            return cls(value)
        raise TypeError(
            "timeout must be a number of seconds, None or a Timeout, "
            f"not {type(value).__name__}"
        )

    def __repr__(self) -> str:
        shown = ", ".join(
            f"{name}={getattr(self, name)!r}"
            for name in _FIELDS
            if getattr(self, name) is not DEFAULT
        )
        return f"Timeout({shown})"


#: What a config's `timeout=` accepts.
_TimeoutArgument = typing.Union[float, None, Timeout]


class _Omitted(enum.Enum):
    """`run(timeout=)` left out: the config's total applies."""

    OMITTED = "omitted"

    def __repr__(self) -> str:
        return "<config timeout>"


_OMITTED = _Omitted.OMITTED


class _TimeoutField:
    """A config's `timeout` attribute: any `_TimeoutArgument` in, a `Timeout` out.

    Assignment (the dataclass `__init__`, `dataclasses.replace`) coerces the
    value and refuses a field the owning config does not support.
    """

    #: The parameter annotation `_keyword_only` shows in `__init__`'s signature.
    init_annotation = _TimeoutArgument

    def __set_name__(self, owner: type, name: str) -> None:
        self._name = name

    @typing.overload
    def __get__(self, instance: None, owner: type) -> Timeout: ...

    @typing.overload
    def __get__(self, instance: object, owner: type) -> Timeout: ...

    def __get__(self, instance: object, owner: type) -> Timeout:
        if instance is None:
            return Timeout()
        value = instance.__dict__.get(self._name)
        return value if isinstance(value, Timeout) else Timeout()

    def __set__(self, instance: object, value: _TimeoutArgument) -> None:
        timeout = Timeout.coerce(value)
        _refuse_unsupported(type(instance), timeout)
        instance.__dict__[self._name] = timeout


def _supported(config_type: type) -> typing.Mapping[str, _Seconds]:
    return typing.cast(
        typing.Mapping[str, _Seconds], getattr(config_type, "_timeout_defaults", {})
    )


def _refuse_unsupported(config_type: type, timeout: Timeout) -> None:
    """Raise `ValueError` for a field `config_type` cannot honour that is set."""
    supported = _supported(config_type)
    for name in _FIELDS:
        if name not in supported and getattr(timeout, name) is not DEFAULT:
            raise ValueError(f"{config_type.__name__}: timeout.{name} is not supported")


def _resolve(timeout: Timeout, defaults: typing.Mapping[str, _Seconds]) -> Timeout:
    """`timeout` with each supported `DEFAULT` replaced by its default.

    A field the transport does not support stays `DEFAULT`, and so does one
    whose default belongs to a composed provider.
    """
    values = {
        name: (
            defaults[name]
            if getattr(timeout, name) is DEFAULT and name in defaults
            else getattr(timeout, name)
        )
        for name in _FIELDS
    }
    return Timeout(
        values["total"],
        connect=values["connect"],
        read=values["read"],
        write=values["write"],
    )


def _config_timeouts(config: object) -> Timeout:
    """`config.timeouts`, or `Timeout()` for a config that has none."""
    timeouts = getattr(config, "timeouts", None)
    return timeouts if isinstance(timeouts, Timeout) else Timeout()


def _run_timeout(
    config: object, timeout: typing.Union[float, None, _Omitted, _Default]
) -> typing.Union[float, None, _Omitted]:
    """The deadline a `run()` call applies.

    The call's own value; else the config's resolved total; `_OMITTED` when
    that total is `DEFAULT`, which leaves the choice to the provider.
    """
    if timeout is not _OMITTED and timeout is not DEFAULT:
        return typing.cast(typing.Optional[float], timeout)
    total = _config_timeouts(config).total
    return _OMITTED if total is DEFAULT else total


#: The URI query key of each field.
_URI_KEYS = {
    "total": "timeout",
    "connect": "timeout_connect",
    "read": "timeout_read",
    "write": "timeout_write",
}

#: Every timeout key a connection URI may carry.
_URI_TIMEOUT_KEYS = tuple(_URI_KEYS.values())


def _seconds_text(value: float) -> str:
    """`20` for 20.0, so a URI keeps its whole-second spelling."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _timeout_query(timeout: Timeout) -> typing.List[typing.Tuple[str, str]]:
    """The URI query pairs of the fields that are not `DEFAULT`; `None` is empty."""
    pairs = []
    for name in _FIELDS:
        value = getattr(timeout, name)
        if value is DEFAULT:
            continue
        pairs.append((_URI_KEYS[name], "" if value is None else _seconds_text(value)))
    return pairs


def _timeout_query_text(timeout: Timeout) -> str:
    """`_timeout_query` encoded, `&`-prefixed when there is anything to add."""
    pairs = _timeout_query(timeout)
    return "&" + urlencode(pairs) if pairs else ""


def _timeout_from_query(query: typing.Mapping[str, object]) -> Timeout:
    """The `Timeout` a parsed URI query spells: absent is `DEFAULT`, empty `None`."""
    values: typing.Dict[str, _Seconds] = {}
    for name, key in _URI_KEYS.items():
        if key not in query:
            values[name] = DEFAULT
            continue
        text = str(query[key])
        if text == "":
            values[name] = None
            continue
        try:
            values[name] = float(text)
        except ValueError as exc:
            raise ValueError(f"{key} must be a number of seconds") from exc
        if not values[name] > 0:  # type: ignore[operator]
            raise ValueError(f"{key} must be greater than 0 seconds")
    return Timeout(
        values["total"],
        connect=values["connect"],
        read=values["read"],
        write=values["write"],
    )


def _refuse_renamed_keys(
    query: typing.Mapping[str, object], renamed: typing.Mapping[str, str]
) -> None:
    """Refuse a URI key that was renamed, naming the key that replaced it."""
    for old, new in renamed.items():
        if old in query:
            raise ValueError(f"unknown connection parameter: {old}; use {new}")


__all__ = ["DEFAULT", "Timeout"]
