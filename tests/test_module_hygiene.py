"""Static checks the formatter does not make.

`black` does not report an unused import, and three had accumulated -- found
by one AST pass, by none of the tooling. A test is the cheapest place to keep
that pass, because it runs where the suite runs.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "src" / "hostctl"
MODULES = sorted(SOURCE.rglob("*.py"))


#: `from __future__ import annotations` binds a name nothing references.
_FUTURE = {"annotations"}


def _imported_names(tree: ast.AST):
    """Names a module binds by importing, excluding deliberate re-exports."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is None:
                    yield alias.name.split(".")[0], node.lineno
                elif alias.asname != alias.name:
                    yield alias.asname, node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                # `name as name` is the explicit re-export spelling this
                # project uses in its package __init__ files.
                if alias.asname == alias.name:
                    continue
                yield (alias.asname or alias.name), node.lineno


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_no_unused_imports(module):
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    # Attribute access reaches the module through its root name.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                used.add(root.id)
    text = module.read_text(encoding="utf-8")
    exported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    exported = {
                        element.value
                        for element in getattr(node.value, "elts", ())
                        if isinstance(element, ast.Constant)
                    }

    unused = [
        (name, line)
        for name, line in _imported_names(tree)
        if name not in used and name not in exported
        # A name used only inside a string annotation still counts.
        and f'"{name}' not in text and f"'{name}" not in text
    ]

    assert unused == [], f"{module.name}: {unused}"


def test_the_command_grammar_does_not_depend_on_host_orchestration():
    """`shell/` is the command grammar and `host/` is orchestration, so the
    grammar importing the orchestrator is upside down -- and it made
    `hostctl.shell` unimportable without `hostctl.host`. A `TYPE_CHECKING`
    import is a type reference, not a runtime dependency, and stays allowed.
    """
    runtime = []
    for module in sorted((SOURCE / "shell").rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        guarded = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test):
                for child in ast.walk(node):
                    guarded.add(getattr(child, "lineno", None))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if not (node.module or "").startswith("host"):
                continue
            if node.lineno in guarded:
                continue
            runtime.append((module.name, node.module, node.lineno))

    assert runtime == [], runtime
