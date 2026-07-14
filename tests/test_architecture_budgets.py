"""R4.2 non-growth budgets and compatibility-facade contracts."""

from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _line_count(relative: str) -> int:
    return len((REPO / relative).read_text(encoding="utf-8").splitlines())


def _class_line_count(relative: str, name: str) -> int:
    tree = ast.parse((REPO / relative).read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == name)
    return int(node.end_lineno or node.lineno) - node.lineno + 1


def test_gradual_size_budgets_prevent_facade_regrowth() -> None:
    assert _line_count("memorymaster/core/service.py") <= 2450
    assert _line_count("memorymaster/surfaces/dashboard.py") <= 1550
    assert _class_line_count("memorymaster/surfaces/dashboard.py", "DashboardRequestHandler") <= 720
    for relative in (
        "memorymaster/core/services/integration.py",
        "memorymaster/surfaces/dashboard_read_models.py",
        "memorymaster/surfaces/dashboard_commands.py",
    ):
        assert _line_count(relative) <= 800


def test_extracted_application_functions_stay_bounded() -> None:
    for relative in (
        "memorymaster/core/services/integration.py",
        "memorymaster/surfaces/dashboard_read_models.py",
        "memorymaster/surfaces/dashboard_commands.py",
    ):
        tree = ast.parse((REPO / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines = int(node.end_lineno or node.lineno) - node.lineno + 1
                assert lines <= 50, f"{relative}:{node.name} grew to {lines} lines"


def test_memory_service_keeps_api_through_extracted_integration_service() -> None:
    from memorymaster.core.service import MemoryService
    from memorymaster.core.services.integration import IntegrationService

    assert issubclass(MemoryService, IntegrationService)
    assert "upsert_source_item" not in MemoryService.__dict__
    assert "create_action_proposal" not in MemoryService.__dict__
    assert "upsert_source_item" in IntegrationService.__dict__
    assert "create_action_proposal" in IntegrationService.__dict__


def test_dashboard_handler_uses_read_model_and_mutation_boundaries() -> None:
    source = (REPO / "memorymaster/surfaces/dashboard.py").read_text(encoding="utf-8")
    assert "from memorymaster.surfaces.dashboard_read_models import" in source
    assert "from memorymaster.surfaces.dashboard_commands import" in source


def test_compatibility_policy_has_a_dated_retirement_gate() -> None:
    policy = (REPO / "docs/compatibility.md").read_text(encoding="utf-8")
    assert "2026-09-30" in policy
    assert "memorymaster.service" in policy
    assert "removal gate" in policy.lower()
