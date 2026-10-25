"""The bulk gate must run the real evaluation fixtures and fail visibly."""
from pathlib import Path


def test_bulk_gate_runs_the_evaluation_suite_and_cannot_skip_it():
    # No hosted CI since 2026-10-25: the local bulk gate runs the whole tests/ tree,
    # which contains the retrieval evaluation and the public demo, with no skip flag
    # for them (only --fast, which is documented as not a merge gate).
    root = Path(__file__).resolve().parents[1]
    gate = (root / "scripts/bulk_check.py").read_text(encoding="utf-8")
    assert '"tests/"' in gate
    assert '"not ml"' in gate
    assert "continue-on-error" not in gate
    for relative in ("tests/test_qrels_regression.py", "tests/test_public_demo.py"):
        assert (root / relative).exists()
    assert "scripts/eval_memorymaster.py" not in gate
    assert (root / "tests/fixtures/qrels_search.json").is_file()
