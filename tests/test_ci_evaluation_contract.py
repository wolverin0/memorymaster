"""The CI evaluation must run real fixtures and fail visibly."""
from pathlib import Path
import yaml


def test_ci_evaluation_is_real_and_required():
    root = Path(__file__).resolve().parents[1]
    job = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]["eval"]
    steps = job["steps"]
    evaluation = next(step for step in steps if step.get("name") == "Run evaluation suite")
    assert not job.get("continue-on-error", False)
    assert not evaluation.get("continue-on-error", False)
    assert "tests/test_qrels_regression.py" in evaluation["run"]
    assert "tests/test_public_demo.py" in evaluation["run"]
    assert "--junitxml=artifacts/eval/results.xml" in evaluation["run"]
    assert "scripts/eval_memorymaster.py" not in evaluation["run"]
    artifact = next(step for step in steps if step.get("name") == "Upload eval artifacts")
    assert artifact["with"]["if-no-files-found"] == "error"
    assert (root / "tests/fixtures/qrels_search.json").is_file()
