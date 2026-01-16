"""Guardrail: command functions must not issue raw SQL queries."""
from __future__ import annotations

import ast
from pathlib import Path


FORBIDDEN_CALLS = {
    "create_safe_db",
    "create_unsafe_db",
}
FORBIDDEN_METHODS = {
    "query",
    "query_one",
    "query_all",
    "execute",
    "execute_returning",
}
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "docs",
    "prototypes",
    "tests",
}


class CommandSQLChecker(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self._filename = filename
        self._cmd_stack: list[str] = []
        self.violations: list[tuple[str, int, int, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name.startswith("cmd_"):
            self._cmd_stack.append(node.name)
            self.generic_visit(node)
            self._cmd_stack.pop()
        else:
            self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._cmd_stack:
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                self._record(node, f"call to {func.id}()")
            elif isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_METHODS:
                self._record(node, f"call to .{func.attr}()")
        self.generic_visit(node)

    def _record(self, node: ast.AST, detail: str) -> None:
        lineno = getattr(node, "lineno", 0)
        col = getattr(node, "col_offset", 0) + 1
        self.violations.append((self._filename, lineno, col, detail))


def _command_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        has_cmd = any(
            isinstance(node, ast.FunctionDef) and node.name.startswith("cmd_")
            for node in ast.walk(tree)
        )
        if has_cmd:
            files.append(path)
    return files


def test_no_raw_sql_in_command_functions() -> None:
    root = Path(__file__).resolve().parents[1]
    violations: list[tuple[str, int, int, str]] = []

    for path in _command_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        checker = CommandSQLChecker(str(path.relative_to(root)))
        checker.visit(tree)
        violations.extend(checker.violations)

    if violations:
        formatted = "\n".join(
            f"{filename}:{lineno}:{col} {detail}"
            for filename, lineno, col, detail in violations
        )
        raise AssertionError(f"Raw SQL usage detected in command functions:\n{formatted}")
