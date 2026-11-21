"""Build-time index of the pinned sources: what kind of code is on each line?

For every Python file under sqlglot/ and tests/ this lists "units": a simple
statement, or the header of a compound one. Each unit knows whether it runs
at import time (module or class body) or inside a function, which function
and classes enclose it, and which names it binds.
"""

from __future__ import annotations

import ast
import os

# Unit tuple layout
START, END, KIND, FUNC, CLASSES, NAMES, INNER = range(7)
# KIND: "m" module-level statement, "c" class-body statement, "f" statement
# inside a function, "d" def header (decorators and signature), "k" class header.


def _targets(node: ast.AST) -> list[str]:
    names: list[str] = []
    if isinstance(node, ast.Name):
        names.append(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            names.extend(_targets(element))
    elif isinstance(node, ast.Starred):
        names.extend(_targets(node.value))
    return names


def _bound_names(node: ast.stmt) -> tuple[str, ...]:
    names: list[str] = []
    if isinstance(node, ast.Assign):
        for target in node.targets:
            names.extend(_targets(target))
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        names.extend(_targets(node.target))
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            names.append(alias.asname or alias.name.split(".")[0])
    return tuple(names)


def _first_line(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        return min(node.lineno, min(d.lineno for d in decorators))
    return node.lineno


def _inner_functions(node: ast.AST) -> tuple[tuple[int, int], ...]:
    """Lambdas (and comprehension-free nested callables) written inside a statement."""
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Lambda):
            found.append((child.lineno, child.end_lineno or child.lineno))
    return tuple(found)


def index_source(source: str) -> dict:
    tree = ast.parse(source)
    units: list[tuple] = []
    functions: list[tuple[int, int]] = []

    def visit(body: list[ast.stmt], kind: str, func: tuple[int, int] | None, classes: tuple[str, ...]):
        for node in body:
            start = _first_line(node)
            end = node.end_lineno or node.lineno
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body_start = _first_line(node.body[0])
                own = (start, end)
                functions.append(own)
                header_end = max(start, body_start - 1) if body_start > node.lineno else node.lineno
                units.append((start, header_end, "d", own, classes, (node.name,), (kind, func)))
                visit(node.body, "f", own, classes)
            elif isinstance(node, ast.ClassDef):
                qualname = (classes[0] + "." if classes else "") + node.name
                body_start = _first_line(node.body[0])
                header_end = max(start, body_start - 1) if body_start > node.lineno else node.lineno
                inside_function = kind == "f"
                units.append((start, header_end, "f" if inside_function else "k", func, (qualname, *classes), (node.name,), ()))
                if inside_function:
                    visit(node.body, "f", func, classes)
                else:
                    visit(node.body, "c", None, (qualname, *classes))
            else:
                blocks = []
                for name in ("body", "orelse", "finalbody"):
                    block = getattr(node, name, None)
                    if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                        blocks.append(block)
                for handler in getattr(node, "handlers", []) or []:
                    blocks.append(handler.body)
                for case in getattr(node, "cases", []) or []:
                    blocks.append(case.body)
                if blocks:
                    first_body = min(_first_line(block[0]) for block in blocks)
                    header_end = max(start, first_body - 1) if first_body > start else start
                    units.append((start, header_end, kind, func, classes, (), _inner_functions(node) if kind != "f" else ()))
                    for block in blocks:
                        visit(block, kind, func, classes)
                else:
                    names = _bound_names(node) if kind != "f" else ()
                    if kind != "f" and isinstance(node, ast.Expr) and isinstance(getattr(node.value, "value", None), str):
                        names = ("__doc__",)  # a docstring: only code reading __doc__ can notice
                    units.append((start, end, kind, func, classes, names, _inner_functions(node) if kind != "f" else ()))

    visit(tree.body, "m", None, ())
    units.sort()
    return {"units": units, "functions": sorted(functions), "lines": source.count("\n") + 1}


def build(root: str) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for top in ("sqlglot", "tests"):
        for directory, _, names in os.walk(os.path.join(root, top)):
            for name in names:
                if name.endswith(".py"):
                    path = os.path.join(directory, name)
                    rel = os.path.relpath(path, root)
                    with open(path, encoding="utf-8") as handle:
                        try:
                            index[rel] = index_source(handle.read())
                        except SyntaxError:
                            index[rel] = {"units": [], "functions": [], "lines": 0}
    return index
