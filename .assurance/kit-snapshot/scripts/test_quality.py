#!/usr/bin/env python3
"""Find Python tests whose bodies contain no recognizable assertion.

This is deliberately a naive structural check: it cannot see assertions
delegated to module-level helpers called by a test, so those helper-driven tests
can be false positives. The spike favors precision but records this unavoidable
boundary explicitly. It uses only the standard library and emits exactly one
JSON object on stdout.
"""

import ast
import json
import sys
from pathlib import Path


def _test_files(repo: Path) -> list[Path]:
    return [
        path
        for path in sorted(repo.rglob("*.py"))
        if path.name.startswith("test_")
        or path.name.endswith("_test.py")
        or "tests" in path.relative_to(repo).parts[:-1]
    ]


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


MOCK_ASSERT_PREFIXES = ("assert_called", "assert_awaited")
MOCK_CONSTRUCTORS = {
    "Mock",
    "MagicMock",
    "AsyncMock",
    "mock.Mock",
    "mock.MagicMock",
    "mock.AsyncMock",
}


def _is_mock_factory(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _dotted_name(node.func)
    return name in MOCK_CONSTRUCTORS or name in {
        "unittest.mock.Mock",
        "unittest.mock.MagicMock",
        "unittest.mock.AsyncMock",
        "patch",
        "patch.object",
        "mock.patch",
        "mock.patch.object",
        "unittest.mock.patch",
        "unittest.mock.patch.object",
    }


class _ExecutableVisitor(ast.NodeVisitor):
    """Visit a test's executable body without entering nested lexical scopes."""

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return None

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None


class _MockNameVisitor(_ExecutableVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Assign(self, node: ast.Assign) -> None:
        if _is_mock_factory(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.names.add(target.id)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value and _is_mock_factory(node.value):
            self.names.add(node.target.id)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if _is_mock_factory(item.context_expr) and isinstance(item.optional_vars, ast.Name):
                self.names.add(item.optional_vars.id)
        self.generic_visit(node)

    visit_AsyncWith = visit_With


def _mock_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    visitor = _MockNameVisitor()
    for statement in node.body:
        visitor.visit(statement)
    return visitor.names


def _is_assertion(node: ast.AST, mock_names: set[str]) -> bool:
    if isinstance(node, ast.Assert):
        return True
    if not isinstance(node, ast.Call):
        return False
    name = _dotted_name(node.func)
    method = name.split(".")[-1]
    receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
    while isinstance(receiver, ast.Attribute):
        receiver = receiver.value
    mock_receiver = isinstance(receiver, ast.Name) and receiver.id in mock_names
    return (
        name.startswith("self.assert")
        or name == "self.fail"
        or name in {"pytest.raises", "pytest.fail", "pytest.warns"}
        or (
            mock_receiver
            and (method.startswith(MOCK_ASSERT_PREFIXES) or method == "assert_not_called")
        )
    )


class _AssertionVisitor(_ExecutableVisitor):
    def __init__(self, mock_names: set[str]) -> None:
        self.mock_names = mock_names
        self.found = False

    def generic_visit(self, node: ast.AST) -> None:
        if _is_assertion(node, self.mock_names):
            self.found = True
            return
        super().generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        for statement in node.body:
            self.visit(statement)


def _has_assertion(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    visitor = _AssertionVisitor(_mock_names(node))
    for statement in node.body:
        visitor.visit(statement)
    return visitor.found


class _TestVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.tests: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name.startswith("test_") and not _is_fixture(node):
            self.tests.append(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node.name.startswith("test_") and not _is_fixture(node):
            self.tests.append(node)


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        name = _dotted_name(decorator.func if isinstance(decorator, ast.Call) else decorator)
        if name in {"fixture", "pytest.fixture"}:
            return True
    return False


def _skip_reason(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for decorator in node.decorator_list:
        name = _dotted_name(decorator.func if isinstance(decorator, ast.Call) else decorator)
        if name in {"pytest.mark.skip", "pytest.mark.skipif"}:
            return f"Skipped test ({name}); skipped tests require separate review."
    return None


def scan(repo: Path) -> dict:
    tests_scanned = 0
    findings = []
    warnings = []
    for path in _test_files(repo):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as error:
            warnings.append({"path": path.relative_to(repo).as_posix(), "warning": str(error)})
            continue
        test_visitor = _TestVisitor()
        test_visitor.visit(tree)
        for node in test_visitor.tests:
            tests_scanned += 1
            skip_reason = _skip_reason(node)
            if skip_reason:
                warnings.append(
                    {
                        "path": path.relative_to(repo).as_posix(),
                        "line": node.lineno,
                        "test": node.name,
                        "warning": skip_reason,
                    }
                )
                continue
            if not _has_assertion(node):
                findings.append(
                    {
                        "path": path.relative_to(repo).as_posix(),
                        "line": node.lineno,
                        "test": node.name,
                        "reason": "No recognized assertion appears in the test body.",
                    }
                )
    next_action = (
        f"Strengthen {findings[0]['test']} in {findings[0]['path']} with a meaningful assertion."
        if findings
        else "No assertion-free tests are flagged."
    )
    return {
        "ok": True,
        "mode": "assertion-scan",
        "language": "python",
        "tests_scanned": tests_scanned,
        "assertion_free": findings,
        "unsupported": [],
        "warnings": warnings,
        "next_action": next_action,
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) < 2 or args[0] != "scan":
            raise ValueError
        repo = Path(args[1])
        language = args[args.index("--language") + 1] if "--language" in args else "python"
    except (IndexError, ValueError):
        print(
            json.dumps(
                {
                    "ok": False,
                    "mode": "assertion-scan",
                    "language": "python",
                    "tests_scanned": 0,
                    "assertion_free": [],
                    "unsupported": [],
                    "warnings": [],
                    "next_action": "Use: test_quality.py scan <repo> [--language python].",
                }
            )
        )
        return 0
    if language != "python":
        result = {
            "ok": True,
            "mode": "assertion-scan",
            "language": language,
            "tests_scanned": 0,
            "assertion_free": [],
            "unsupported": [language],
            "warnings": [],
            "next_action": f"Language {language} is unsupported; use --language python.",
        }
    else:
        result = scan(repo)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
