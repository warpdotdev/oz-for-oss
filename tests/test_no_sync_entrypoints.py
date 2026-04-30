from __future__ import annotations

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_AGENT_WORKFLOW_SCRIPTS = [
    "core/workflows/review_pr.py",
    "core/workflows/respond_to_pr_comment.py",
    "core/workflows/verify_pr_comment.py",
    "core/workflows/triage_new_issues.py",
]


def _is_dunder_main_guard(node: ast.If) -> bool:
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def test_agent_workflow_scripts_have_no_direct_main_entrypoints() -> None:
    for relative_path in _AGENT_WORKFLOW_SCRIPTS:
        source_path = _REPO_ROOT / relative_path
        tree = ast.parse(source_path.read_text(), filename=str(source_path))
        assert not any(
            isinstance(node, ast.If) and _is_dunder_main_guard(node)
            for node in ast.walk(tree)
        ), relative_path


def test_agent_workflow_scripts_do_not_call_run_agent_directly() -> None:
    for relative_path in _AGENT_WORKFLOW_SCRIPTS:
        source_path = _REPO_ROOT / relative_path
        tree = ast.parse(source_path.read_text(), filename=str(source_path))
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_agent"
            for node in ast.walk(tree)
        ), relative_path
