"""The local executor and path providers."""

from __future__ import annotations

import logging

import os

import typing

from pathapi import Path


from ...provider._contracts import ExecutorProvider, PathProvider, ProviderProbe

from ...provider._contracts import FULL_PATH_OPERATIONS

log = logging.getLogger("hostctl.transports.local")


class LocalExecutorProvider(ExecutorProvider):
    """Local subprocess execution with native argv, cwd, and env support."""

    def __init__(self, executor=None):
        if executor is None:
            from ._executor import LocalExecutor

            executor = LocalExecutor()
        self.transport = executor
        super().__init__("local", executor)

    def probe(self) -> ProviderProbe:
        return ProviderProbe(
            "available", capabilities=self.capabilities, system_hint=os.name
        )


class LocalPathProvider(PathProvider):
    """Local filesystem paths; the process's own filesystem is always usable."""

    def __init__(self, factory: typing.Optional[typing.Callable[..., Path]] = None):
        super().__init__(
            "local",
            factory if factory is not None else (lambda *segments: Path(*segments)),
            capabilities=FULL_PATH_OPERATIONS,
        )

    def probe(self) -> ProviderProbe:
        return ProviderProbe(
            "available", capabilities=self.capabilities, system_hint=os.name
        )
