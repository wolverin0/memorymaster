"""DecisionConfig: env parsing, per-surface overrides, fail-safe defaults."""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.decisions import config as cfg
from memorymaster.decisions.config import SURFACES, DecisionConfig, env_truthy


def test_defaults_are_off_and_bounded():
    c = DecisionConfig.from_env({})
    assert c.global_mode == "off"
    assert all(c.mode_for(s) == "off" for s in SURFACES)
    assert c.daily_usd_cap == 2.0
    assert c.rpm_cap == 600
    assert c.hook_deadline_ms == 900
    assert c.batch_deadline_ms == 8000
    assert c.state_retention_days == 180
    assert c.log_off is False
    assert c.explore_rate("recall") == pytest.approx(0.10)
    assert c.explore_rate("ingest") == pytest.approx(0.05)
    for surface in SURFACES:
        if surface not in {"recall", "ingest"}:
            assert c.explore_rate(surface) == 0.0
    assert c.decisions_db == Path.home() / ".memorymaster" / "decisions.db"


def test_surfaces_are_the_eight_contract_ids():
    assert SURFACES == ("revalidate", "recall", "ingest", "dedup", "skills", "hints", "route", "session")


def test_global_mode_and_per_surface_override():
    env = {"MEMORYMASTER_JEV_MODE": "live", "MEMORYMASTER_JEV_RECALL": "shadow", "MEMORYMASTER_JEV_INGEST": "OFF"}
    c = DecisionConfig.from_env(env)
    assert c.mode_for("recall") == "shadow"
    assert c.mode_for("ingest") == "off"
    assert c.mode_for("revalidate") == "live"
    assert c.mode_for("RECALL") == "shadow"


def test_invalid_modes_fail_safe_to_off():
    c = DecisionConfig.from_env({"MEMORYMASTER_JEV_MODE": "yolo", "MEMORYMASTER_JEV_RECALL": "on"})
    assert c.global_mode == "off"
    assert c.mode_for("recall") == "off"
    # A broken per-surface value must not inherit a live global mode either.
    c = DecisionConfig.from_env({"MEMORYMASTER_JEV_MODE": "live", "MEMORYMASTER_JEV_RECALL": "livee"})
    assert c.mode_for("recall") == "off"
    assert c.mode_for("ingest") == "live"


def test_unknown_surface_is_off():
    c = DecisionConfig.from_env({"MEMORYMASTER_JEV_MODE": "live"})
    assert c.mode_for("not-a-surface") == "off"
    assert c.explore_rate("not-a-surface") == 0.0


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on", "On"])
def test_truthiness_accepts_contract_values(raw):
    assert env_truthy(raw) is True


@pytest.mark.parametrize("raw", [None, "", "0", "false", "no", "off", "y", "2", "enabled"])
def test_truthiness_rejects_everything_else(raw):
    assert env_truthy(raw) is False


def test_log_off_uses_shared_truthiness():
    assert DecisionConfig.from_env({"MEMORYMASTER_DECISIONS_LOG_OFF": "yes"}).log_off is True
    assert DecisionConfig.from_env({"MEMORYMASTER_DECISIONS_LOG_OFF": "y"}).log_off is False


def test_numeric_overrides_and_invalid_numbers_keep_defaults(tmp_path):
    env = {
        "MEMORYMASTER_JEV_DAILY_USD_CAP": "0.5",
        "MEMORYMASTER_JEV_RPM_CAP": "30",
        "MEMORYMASTER_JEV_HOOK_DEADLINE_MS": "250",
        "MEMORYMASTER_JEV_BATCH_DEADLINE_MS": "1000",
        "MEMORYMASTER_JEV_EXPLORE_RECALL": "0.2",
        "MEMORYMASTER_JEV_EXPLORE_DEDUP": "0.3",
        "MEMORYMASTER_DECISIONS_DB": str(tmp_path / "d.db"),
        "MEMORYMASTER_DECISIONS_STATE_RETENTION_DAYS": "7",
    }
    c = DecisionConfig.from_env(env)
    assert c.daily_usd_cap == 0.5
    assert c.rpm_cap == 30
    assert c.hook_deadline_ms == 250
    assert c.batch_deadline_ms == 1000
    assert c.explore_rate("recall") == pytest.approx(0.2)
    assert c.explore_rate("dedup") == pytest.approx(0.3)
    assert c.decisions_db == tmp_path / "d.db"
    assert c.state_retention_days == 7

    bad = {
        "MEMORYMASTER_JEV_DAILY_USD_CAP": "nan",
        "MEMORYMASTER_JEV_RPM_CAP": "-5",
        "MEMORYMASTER_JEV_HOOK_DEADLINE_MS": "fast",
        "MEMORYMASTER_JEV_EXPLORE_RECALL": "1.5",
        "MEMORYMASTER_JEV_EXPLORE_INGEST": "inf",
        "MEMORYMASTER_DECISIONS_STATE_RETENTION_DAYS": "0",
    }
    c = DecisionConfig.from_env(bad)
    assert c.daily_usd_cap == 2.0
    assert c.rpm_cap == 600
    assert c.hook_deadline_ms == 900
    assert c.explore_rate("recall") == pytest.approx(0.10)
    assert c.explore_rate("ingest") == pytest.approx(0.05)
    assert c.state_retention_days == 180


def test_from_env_reads_process_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORYMASTER_JEV_MODE", "shadow")
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(tmp_path / "x.db"))
    c = DecisionConfig.from_env()
    assert c.mode_for("hints") == "shadow"
    assert c.decisions_db == tmp_path / "x.db"


def test_env_names_are_exported_for_docs():
    assert cfg.MODE_ENV == "MEMORYMASTER_JEV_MODE"
    assert cfg.surface_env("recall") == "MEMORYMASTER_JEV_RECALL"
    assert cfg.explore_env("ingest") == "MEMORYMASTER_JEV_EXPLORE_INGEST"


def test_hook_busy_and_breaker_probe_settings():
    c = DecisionConfig.from_env({})
    assert c.hook_busy_ms == 250 and c.breaker_probe_s == 60
    c = DecisionConfig.from_env({"MEMORYMASTER_DECISIONS_HOOK_BUSY_MS": "80", "MEMORYMASTER_JEV_BREAKER_PROBE_S": "5"})
    assert c.hook_busy_ms == 80 and c.breaker_probe_s == 5
    for bad in ("0", "-3", "soon", ""):
        c = DecisionConfig.from_env({"MEMORYMASTER_DECISIONS_HOOK_BUSY_MS": bad, "MEMORYMASTER_JEV_BREAKER_PROBE_S": bad})
        assert c.hook_busy_ms == 250 and c.breaker_probe_s == 60
