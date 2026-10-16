"""``hostctl info``: the host's identity as JSON, or one line per host."""

from __future__ import annotations

import dataclasses
import json
import sys
import typing

from ._common import write
from ._fanout import FanOutCommand


class Info(FanOutCommand):
    """Print the host's identity as JSON.

    On several hosts, one JSON object per line: {"target": URI, "result": {...}}.
    """

    _parsername_ = "info"

    def __call__(self) -> int:
        targets = self.targets()
        if len(targets) > 1:
            return self.fan_out(targets, _identity, _emit)
        with self.open(targets[0], self.credentials()) as host:
            value = _identity(host)
        write(sys.stdout, json.dumps(value, sort_keys=True) + "\n")
        return 0


def _identity(host: typing.Any) -> typing.Dict[str, typing.Any]:
    # `info()` falls back to the config for what no provider answered. The
    # exit status must not: a host nothing could reach is not a success that
    # happens to know its own name.
    collect = getattr(host, "_collect_info", None)
    info, answered, failures = collect() if collect else (host.info(), True, [])
    if failures and not answered:
        raise failures[0]
    if dataclasses.is_dataclass(info):
        return dataclasses.asdict(info)
    return vars(info)


def _emit(label: str, value: typing.Dict[str, typing.Any], out, err) -> int:
    record = {"target": label, "result": value}
    write(out, json.dumps(record, sort_keys=True) + "\n")
    return 0


__all__ = [
    "Info",
]
