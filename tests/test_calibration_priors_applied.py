from __future__ import annotations

import json
from pathlib import Path

from memorymaster.core.config import Config


def test_config_initial_confidence_priors_match_calibration_report() -> None:
    report_path = Path(__file__).resolve().parents[1] / "docs" / "calibration-priors-2026-05-11.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    expected_by_type = {
        row["claim_type"]: row["recommended_initial_confidence"]
        for row in report["priors"]
    }

    cfg = Config()

    for claim_type in ("fact", "decision", "constraint"):
        assert abs(cfg.initial_confidence_by_type[claim_type] - expected_by_type[claim_type]) <= 0.01
