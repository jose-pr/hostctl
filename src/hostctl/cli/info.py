"""``hostctl info``: the host's identity as JSON."""

from __future__ import annotations

import dataclasses
import json
import sys

from ._common import HostCommand, write


class Info(HostCommand):
    """Print the host's identity as JSON."""

    _parsername_ = "info"

    def __call__(self) -> int:
        with self.connect() as host:
            # `info()` falls back to the config for what no provider answered.
            # The exit status must not: a host nothing could reach is not a
            # success that happens to know its own name.
            collect = getattr(host, "_collect_info", None)
            info, answered, failures = collect() if collect else (host.info(), True, [])
            if failures and not answered:
                raise failures[0]
        if dataclasses.is_dataclass(info):
            value = dataclasses.asdict(info)
        else:
            value = vars(info)
        write(sys.stdout, json.dumps(value, sort_keys=True) + "\n")
        return 0


__all__ = [
    "Info",
]
