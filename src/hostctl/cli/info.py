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
            info = host.info()
        if dataclasses.is_dataclass(info):
            value = dataclasses.asdict(info)
        else:
            value = vars(info)
        write(sys.stdout, json.dumps(value, sort_keys=True) + "\n")
        return 0
