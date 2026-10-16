"""Composite paths: one logical path routed over a host's path providers."""

from ._composite import (
    CompositePosixPath as CompositePosixPath,
    CompositeWindowsPath as CompositeWindowsPath,
)

__all__ = ["CompositePosixPath", "CompositeWindowsPath"]
