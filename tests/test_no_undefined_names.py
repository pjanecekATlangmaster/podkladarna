"""Statická pojistka: funkce nesmí používat jména, která nikde nedefinují.

Chyba ``NameError: name 'dtm_prefetch' is not defined`` (proměnná z jedné funkce použitá
v jiné) prošla testy, protože ji žádný test nespustil. Tahle kontrola projde AST
vybraných modulů a každé použité jméno musí být lokální (parametr, přiřazení, import,
def/class, cyklus, with, výjimka, comprehension), modulové nebo vestavěné.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app" / "pipeline"
MODULES = [
    "run_job.py",
    "package_oom.py",
    "osm_paths.py",
    "oom_import.py",
    "residual_paved.py",
    "reference_layers.py",
    "dtm_import.py",
    "dtm_sources.py",
    "dtm_fences.py",
    "jvf_reader.py",
]
_BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}


def _stores(node: ast.AST) -> set[str]:
    """Všechna jména, která se uvnitř ``node`` (vč. vnořených funkcí) někde vážou."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
        elif isinstance(n, ast.MatchAs) and n.name:
            out.add(n.name)
    return out


def _module_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        else:
            names |= _stores(stmt)
    return names


def _undefined(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module = _module_names(tree)
    # obklopující funkce každé funkce, spočtené jedním průchodem (dřív O(n²) přes ast.walk)
    ancestors: dict[ast.AST, list[ast.AST]] = {}

    def visit(node: ast.AST, stack: list[ast.AST]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ancestors[child] = stack
                visit(child, stack + [child])
            else:
                visit(child, stack)

    visit(tree, [])
    stores = {func: _stores(func) for func in ancestors}
    problems: list[str] = []
    for func, parents in ancestors.items():
        defined = stores[func] | module | _BUILTINS
        # vnořená funkce vidí i proměnné obklopujících funkcí – přidej všechny předky
        for parent in parents:
            defined |= stores[parent]
        for n in ast.walk(func):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in defined:
                problems.append(f"{path.name}:{n.lineno} {func.name}(): '{n.id}'")
    return sorted(set(problems))


@pytest.mark.parametrize("module", MODULES)
def test_no_undefined_names_in_pipeline_functions(module):
    problems = _undefined(ROOT / module)
    assert not problems, "Nedefinovaná jména:\n" + "\n".join(problems)


def test_checker_catches_the_original_bug(tmp_path):
    src = tmp_path / "m.py"
    src.write_text(
        "def a():\n    x = 1\n    return x\n\n"
        "def b():\n    if x is not None:\n        return 2\n",
        encoding="utf-8",
    )
    found = _undefined(src)
    assert found == ["m.py:6 b(): 'x'"]
