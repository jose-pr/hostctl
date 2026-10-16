"""Comments, docstrings and docs describe the code as it is.

What changed and why belongs in the commit message and the changelog; a
comment narrating it goes stale, and one citing a record outside the
repository sends the reader somewhere they cannot follow. This walks the
tracked text trees and fails on either kind of line.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Directories walked in full, and single files at the repository root.
_TREES = ("src", "tests", "docs", "examples", "benchmarks", ".github")
_FILES = (
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "pyproject.toml",
    "mkdocs.yml",
    ".gitignore",
)

#: Records of the past, and this file, which spells the patterns.
_EXCLUDED = {"CHANGELOG.md", "docs/changelog.md", "tests/test_comment_hygiene.py"}

_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".txt", ".cfg"}

# The private names this test forbids are built from parts, so the repository's
# own leak scan does not flag the rule that enforces them.
_PRIVATE_DIR = "." + "agents"

_FORBIDDEN = re.compile(
    re.escape(_PRIVATE_DIR + "/") + r"|kb/[A-Z]+\.md"
    r"|summarize_run"
    r"|\bD[0-9]{2}\b"
    r"|(?i:\bphase [0-9])"
    r"|\b(core|container|composite|ssh|winrm|cqs|shell|standards|docs|qemu"
    r"|serial|pytruenas)-v?[0-9]{2}\b"
    r"|(?i:\b(previously|for years|used to|no longer)\b)"
)

#: `(path, substring)` pairs that match a pattern and are correct as written.
_ALLOWED = {
    # The ignore rule for the private working directory has to name it.
    (".gitignore", f"`{_PRIVATE_DIR}/` pattern"),
}


def _files():
    for name in _FILES:
        path = ROOT / name
        if path.is_file():
            yield path
    for tree in _TREES:
        base = ROOT / tree
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if (
                path.is_file()
                and path.suffix in _SUFFIXES
                and "__pycache__" not in path.parts
            ):
                yield path


def _hits():
    for path in _files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in _EXCLUDED:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if _FORBIDDEN.search(line):
                yield rel, number, line


def _allowed(rel: str, line: str) -> bool:
    return any(rel == path and part in line for path, part in _ALLOWED)


def test_no_comment_narrates_history_or_cites_a_private_record():
    offenders = [
        f"{rel}:{number}: {line.strip()}"
        for rel, number, line in _hits()
        if not _allowed(rel, line)
    ]
    assert offenders == []


def test_every_allowance_is_still_needed():
    used = {
        (path, part)
        for rel, _, line in _hits()
        for path, part in _ALLOWED
        if rel == path and part in line
    }
    assert _ALLOWED - used == set()


@pytest.mark.parametrize(
    "line",
    [
        "# This previously returned None.",
        "# It used to raise here.",
        "# The cache no longer holds it.",
        "# Unreachable for years.",
        f"# See {_PRIVATE_DIR}/notes.md.",
        "# Per kb/PYTHON.md.",
        "# Recorded as D" "07.",
        "# Done in Phase" " 3.",
        "# core-01 lived here.",
        "# container-14: errors without encoding.",
        "# summarize_run writes a log.",
    ],
)
def test_the_patterns_catch_what_they_are_for(line):
    assert _FORBIDDEN.search(line)


@pytest.mark.parametrize(
    "line",
    [
        "# The server refused to start this command.",
        "# Defined in RFC 3986, section 3.2.",
        "# The QEMU guest agent answers guest-exec.",
    ],
)
def test_the_patterns_leave_ordinary_text_alone(line):
    assert not _FORBIDDEN.search(line)
