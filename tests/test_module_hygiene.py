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


#: Compiler-generated names (3.14 annotation scopes) no source line spells.
_COMPILER_NAMES = {"__conditional_annotations__"}
_MODULE_DUNDERS = {
    "__file__",
    "__name__",
    "__doc__",
    "__spec__",
    "__path__",
    "__builtins__",
    "__loader__",
    "__package__",
    "__annotations__",
    "__class__",
}


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_no_module_references_an_undefined_global(module):
    """A name used in a function body but bound nowhere is a `NameError` that
    fires only when that line runs; one such line broke every argv call through
    a composed host's executor."""
    import builtins
    import symtable

    top = symtable.symtable(module.read_text(encoding="utf-8"), str(module), "exec")
    defined = {
        s.get_name()
        for s in top.get_symbols()
        if s.is_assigned() or s.is_imported() or s.is_namespace()
    }

    def collect(table):
        for symbol in table.get_symbols():
            if symbol.is_declared_global() and symbol.is_assigned():
                defined.add(symbol.get_name())
        for child in table.get_children():
            collect(child)

    collect(top)
    allowed = set(dir(builtins)) | _MODULE_DUNDERS | _COMPILER_NAMES
    missing = set()

    def scan(table):
        for symbol in table.get_symbols():
            if symbol.is_referenced() and (
                symbol.is_global()
                or (
                    table is top
                    and not symbol.is_assigned()
                    and not symbol.is_imported()
                )
            ):
                name = symbol.get_name()
                if name not in defined and name not in allowed:
                    missing.add(name)
        for child in table.get_children():
            scan(child)

    scan(top)
    assert missing == set(), f"{module.name}: {sorted(missing)}"


#: What a fresh interpreter reports before and after `import hostctl`.
_SIDE_EFFECT_PROBE = """
import json, os, socket, sys
before = (hasattr(os, "sysconf"), hasattr(socket.socket, "recvmsg"), sorted(dir(socket)))
import hostctl
after = (hasattr(os, "sysconf"), hasattr(socket.socket, "recvmsg"), sorted(dir(socket)))
print(json.dumps({
    "changed": [list(map(str, b)) for b in zip(before, after) if b[0] != b[1]],
    "modules": sorted({name.split(".")[0] for name in sys.modules}),
}))
"""

#: Optional dependencies and the CLI framework `import hostctl` must not load.
_NOT_ON_IMPORT = (
    "netimps",
    "asyncssh",
    "sftppath",
    "winrm",
    "pypsrp",
    "docker",
    "serial",
    "libvirt",
    "duho",
)


def _probe_import(extra_env=None):
    import json
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(SOURCE.parent), env.get("PYTHONPATH")))
    )
    env.update(extra_env or {})
    # Not `-I`: isolated mode ignores PYTHONPATH, so the child would import
    # an installed hostctl instead of this checkout.
    result = subprocess.run(
        [sys.executable, "-c", _SIDE_EFFECT_PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_import_has_no_process_side_effects():
    """`import hostctl` patches no stdlib module and loads no optional
    dependency: netimps, for one, patches `socket` and `os` on Windows at its
    own import."""
    report = _probe_import()

    assert report["changed"] == []
    assert sorted(set(_NOT_ON_IMPORT) & set(report["modules"])) == []


def test_import_does_not_depend_on_netimps_environment():
    """netimps refuses to import while `NETIMPS_NO_SOCKET_PATCH` is set; a
    package import that reached it would fail the same way."""
    report = _probe_import({"NETIMPS_NO_SOCKET_PATCH": "1"})

    assert report["changed"] == []


def _dotted(module: pathlib.Path) -> str:
    parts = list(module.relative_to(SOURCE.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


#: Modules a caller may import: no path component starts with an underscore.
PUBLIC = [
    module
    for module in MODULES
    if not any(part.startswith("_") for part in _dotted(module).split(".")[1:])
]


def _declared_all(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                return {
                    element.value
                    for element in getattr(node.value, "elts", ())
                    if isinstance(element, ast.Constant)
                }
    return None


@pytest.mark.parametrize("module", PUBLIC, ids=_dotted)
def test_every_public_module_declares_all(module):
    """In a `py.typed` package a module without an underscore promises its
    import path, and `__all__` is the list of names it promises."""
    tree = ast.parse(module.read_text(encoding="utf-8"))

    assert _declared_all(tree) is not None, f"{_dotted(module)} has no __all__"


@pytest.mark.parametrize("module", MODULES, ids=_dotted)
def test_an_explicit_reexport_is_in_all(module):
    """`import X as X` is the typing spec's re-export spelling: outside
    `__all__` it publishes a name the module does not mean to."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    declared = _declared_all(tree) or set()
    stray = [
        alias.name
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
        if alias.asname == alias.name and alias.asname not in declared
    ]

    assert stray == [], f"{_dotted(module)}: {stray}"


#: Contract modules: none may import a transport or the system hosts.
_CONTRACTS = (
    "exceptions",
    "host",
    "uri",
    "_registry",
    "provider",
    "executor",
    "process",
    "paths",
    "shell",
    "sync",
)


def _module_level_imports(module: pathlib.Path):
    """`(target, line)` for every import a module runs when it is imported."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    dotted = _dotted(module)
    package = dotted if module.name == "__init__.py" else dotted.rpartition(".")[0]

    def visit(body):
        for node in body:
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test):
                yield from visit(node.orelse)
            elif isinstance(node, (ast.If, ast.Try)):
                yield from visit(node.body)
                yield from visit(node.orelse)
                for handler in getattr(node, "handlers", []):
                    yield from visit(handler.body)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package.split(".")
                    base = base[: len(base) - (node.level - 1)]
                    target = ".".join(base + ([node.module] if node.module else []))
                else:
                    target = node.module or ""
                for alias in node.names:
                    yield f"{target}.{alias.name}", node.lineno
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    yield alias.name, node.lineno

    yield from visit(tree.body)


def _component(module: pathlib.Path) -> str:
    parts = _dotted(module).split(".")
    return parts[1] if len(parts) > 1 else ""


@pytest.mark.parametrize(
    "module", [m for m in MODULES if _component(m) in _CONTRACTS], ids=_dotted
)
def test_contracts_do_not_import_transports_or_system(module):
    """The contracts sit below every transport, so one importing a transport
    at module level is a cycle waiting for the next split. The command
    grammar in `shell/` also stays below the composite paths."""
    forbidden = ("hostctl.transports", "hostctl.system")
    if _component(module) == "shell":
        forbidden += ("hostctl.paths",)
    hits = [
        (target, line)
        for target, line in _module_level_imports(module)
        if target.startswith(forbidden)
    ]

    assert hits == [], f"{_dotted(module)}: {hits}"
