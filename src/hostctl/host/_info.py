"""Normalized system information a host reports."""

from __future__ import annotations


import dataclasses as _dc


import typing as _ty


@_dc.dataclass(frozen=True)
class HostInfo:
    """Normalized system information; unavailable values remain ``None``."""

    hostname: _ty.Optional[str] = None
    os_family: _ty.Optional[str] = None
    os_name: _ty.Optional[str] = None
    os_version: _ty.Optional[str] = None
    architecture: _ty.Optional[str] = None


def parse_host_info(output: _ty.Union[bytes, str, None]) -> HostInfo:
    """Parse newline-delimited ``HostInfo`` fields from a transport response."""
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    values = {}
    for line in (output or "").splitlines():
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if separator and key in HostInfo.__dataclass_fields__ and value:
            values[key] = value
    if "os_family" in values:
        values["os_family"] = normalize_os_family(values["os_family"])
    return HostInfo(**values)


def normalize_os_family(value: _ty.Optional[str]) -> _ty.Optional[str]:
    """Normalize a directly reported OS family without inferring one."""
    if not value:
        return None
    normalized = value.casefold()
    aliases = {
        "win32nt": "windows",
        "windows": "windows",
        "linux": "linux",
        "darwin": "macos",
        "macos": "macos",
    }
    return aliases.get(normalized, normalized)
