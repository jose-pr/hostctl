"""The extras probes and the install hint read one table."""

from __future__ import annotations

import sys

import pytest

import hostctl
from hostctl import _extras


def test_the_probes_are_exported_and_import_nothing(monkeypatch):
    for name in ("asyncssh", "sftppath", "pypsrp"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    hostctl.has_ssh()
    hostctl.has_psrp()

    assert not {"asyncssh", "sftppath", "pypsrp"} & set(sys.modules)


def test_a_hidden_pypsrp_is_reported_and_named(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypsrp", None)

    assert hostctl.has_psrp() is False
    # Below Python 3.10 the floor is what refuses; the extra is named either way.
    expected = (
        r"'psrp' extra: pip install \"hostctl\[psrp\]\""
        if sys.version_info >= (3, 10)
        else r"3\.10 or newer \(the 'psrp' extra"
    )
    with pytest.raises(ImportError, match=expected):
        _extras._require("psrp", "PSRP")


def test_an_extra_is_unavailable_below_its_python_floor(monkeypatch):
    names, _ = _extras._EXTRAS["psrp"]
    monkeypatch.setitem(_extras._EXTRAS, "psrp", (names, (99, 0)))

    assert hostctl.has_psrp() is False
    with pytest.raises(ImportError, match="99.0"):
        _extras._require("psrp", "PSRP")


def test_a_hidden_sftppath_makes_the_ssh_extra_incomplete(monkeypatch):
    monkeypatch.setitem(sys.modules, "sftppath", None)

    assert hostctl.has_ssh() is False
    assert _extras._missing("ssh") == ("sftppath",)
    with pytest.raises(
        ImportError, match=r"'ssh' extra: pip install \"hostctl\[ssh\]\""
    ):
        _extras._require("ssh", "SSH")


def test_every_extra_in_the_table_is_declared_in_pyproject():
    import pathlib
    import re

    text = (pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    section = text.split("[project.optional-dependencies]", 1)[1].split("\n[", 1)[0]
    declared = set(re.findall(r"^([A-Za-z0-9_-]+)\s*=", section, re.M))

    assert set(_extras._EXTRAS) <= declared
