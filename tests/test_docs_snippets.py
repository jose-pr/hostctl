"""The documentation's code has to be code.

A snippet nobody executes is prose that looks executable. Three documented
`run([...])` calls raised `ValueError` at the shell layer -- a `"%s\\n"`
element carries a newline, and a structured element is a VALUE, where a
newline is how a second command gets smuggled in. Nothing caught it because
no test had ever looked at a fence.

Two checks, both cheap and neither needing a transport:

1. every ```python fence compiles;
2. every literal argument list handed to `run(...)`/`send(...)` in a fence
   is rendered by a real `ShellFlavour`, which is exactly the call the
   library would make.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from hostctl.shell import POSIX_SHELL, POWERSHELL

ROOT = Path(__file__).resolve().parents[1]

_FENCE = re.compile(r"^```python\n(.*?)^```", re.DOTALL | re.MULTILINE)


def _documents():
    return [
        ROOT / "README.md",
        ROOT / "docs" / "index.md",
        *sorted((ROOT / "docs" / "guide").glob("*.md")),
    ]


def _fences(document):
    text = document.read_text(encoding="utf-8")
    return list(_FENCE.finditer(text))


def _document_id(document):
    return str(document.relative_to(ROOT)).replace("\\", "/")


@pytest.mark.parametrize("document", _documents(), ids=_document_id)
def test_every_python_fence_compiles(document):
    for match in _fences(document):
        snippet = match.group(1)
        line = document.read_text(encoding="utf-8")[: match.start()].count("\n") + 2
        try:
            compile(snippet, f"{_document_id(document)}:{line}", "exec")
        except SyntaxError as exc:
            raise AssertionError(
                f"{_document_id(document)}:{line} does not compile: {exc}"
            ) from exc


def _structured_arguments(snippet):
    """Yield every literal list/tuple passed to `run(...)` or `send(...)`."""
    try:
        tree = ast.parse(snippet)
    except SyntaxError:  # reported by the compile test above
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name not in {"run", "send", "spawn", "execute"}:
            continue
        for argument in node.args:
            if not isinstance(argument, (ast.List, ast.Tuple)):
                continue
            if not all(
                isinstance(element, ast.Constant) and isinstance(element.value, str)
                for element in argument.elts
            ):
                continue
            yield [element.value for element in argument.elts]


@pytest.mark.parametrize("document", _documents(), ids=_document_id)
def test_every_documented_structured_command_renders(document):
    text = document.read_text(encoding="utf-8")
    for match in _fences(document):
        line = text[: match.start()].count("\n") + 2
        for argv in _structured_arguments(match.group(1)):
            for flavour in (POSIX_SHELL, POWERSHELL):
                try:
                    flavour.script((tuple(argv),))
                except Exception as exc:
                    raise AssertionError(
                        f"{_document_id(document)}:{line} {argv!r} cannot be "
                        f"rendered by {flavour.name}: {exc}"
                    ) from exc
