"""Tests for memorymaster.config — centralized tunable constants."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from memorymaster.config import Config, get_config, load_config, reset_config, set_config


@pytest.fixture(autouse=True)
def _reset():
    """Ensure each test starts with a clean config singleton."""
    reset_config()
    yield
    reset_config()


class TestConfigDefaults:
    def test_default_retrieval_weights(self):
        cfg = Config()
        assert cfg.retrieval_weights == (0.45, 0.30, 0.15, 0.10)

    def test_default_no_vector_weights(self):
        cfg = Config()
        assert cfg.retrieval_weights_no_vector == (0.55, 0.30, 0.15)

    def test_default_lexical_weights(self):
        cfg = Config()
        assert cfg.lexical_weights == (0.55, 0.15, 0.25, 0.05)

    def test_default_freshness_half_life(self):
        cfg = Config()
        assert cfg.freshness_half_life_hours == {"low": 168.0, "medium": 72.0, "high": 24.0}

    def test_default_cadence_hours(self):
        cfg = Config()
        assert cfg.cadence_hours == {"low": 168.0, "medium": 72.0, "high": 24.0}

    def test_default_decay_rates(self):
        cfg = Config()
        assert cfg.decay_rates == {"low": 0.005, "medium": 0.02, "high": 0.05}

    def test_default_thresholds(self):
        cfg = Config()
        assert cfg.validation_threshold == 0.58
        assert cfg.stale_threshold == 0.35
        assert cfg.conflict_margin == 0.08
        assert cfg.pinned_bonus == 0.03


class TestConfigEnvOverrides:
    def test_retrieval_weights_env(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_RETRIEVAL_WEIGHTS", "0.40,0.25,0.20,0.15")
        cfg = load_config()
        assert cfg.retrieval_weights == (0.40, 0.25, 0.20, 0.15)

    def test_retrieval_weights_no_vector_env(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_RETRIEVAL_WEIGHTS_NO_VECTOR", "0.60,0.25,0.15")
        cfg = load_config()
        assert cfg.retrieval_weights_no_vector == (0.60, 0.25, 0.15)

    def test_lexical_weights_env(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_LEXICAL_WEIGHTS", "0.50,0.20,0.20,0.10")
        cfg = load_config()
        assert cfg.lexical_weights == (0.50, 0.20, 0.20, 0.10)

    def test_freshness_halflife_env(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_FRESHNESS_HALFLIFE", "200.0,100.0,48.0")
        cfg = load_config()
        assert cfg.freshness_half_life_hours == {"low": 200.0, "medium": 100.0, "high": 48.0}

    def test_cadence_hours_env(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_CADENCE_HOURS", "336.0,144.0,48.0")
        cfg = load_config()
        assert cfg.cadence_hours == {"low": 336.0, "medium": 144.0, "high": 48.0}

    def test_decay_rates_env(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_DECAY_RATES", "0.02,0.05,0.10")
        cfg = load_config()
        assert cfg.decay_rates == {"low": 0.02, "medium": 0.05, "high": 0.10}

    def test_validation_threshold_env(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_VALIDATION_THRESHOLD", "0.75")
        cfg = load_config()
        assert cfg.validation_threshold == 0.75

    def test_stale_threshold_env(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_STALE_THRESHOLD", "0.40")
        cfg = load_config()
        assert cfg.stale_threshold == 0.40

    def test_conflict_margin_env(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_CONFLICT_MARGIN", "0.12")
        cfg = load_config()
        assert cfg.conflict_margin == 0.12

    def test_pinned_bonus_env(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_PINNED_BONUS", "0.05")
        cfg = load_config()
        assert cfg.pinned_bonus == 0.05

    def test_bad_count_raises(self, monkeypatch):
        monkeypatch.setenv("MEMORYMASTER_RETRIEVAL_WEIGHTS", "0.5,0.5")
        with pytest.raises(ValueError, match="Expected 4"):
            load_config()


class TestConfigJsonFile:
    def test_json_override(self):
        data = {"validation_threshold": 0.90, "pinned_bonus": 0.07}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            cfg = load_config(config_path=f.name)
        os.unlink(f.name)
        assert cfg.validation_threshold == 0.90
        assert cfg.pinned_bonus == 0.07
        # Defaults preserved for non-overridden fields
        assert cfg.stale_threshold == 0.35

    def test_json_via_env(self, monkeypatch):
        data = {"conflict_margin": 0.20}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            monkeypatch.setenv("MEMORYMASTER_CONFIG_FILE", f.name)
            cfg = load_config()
        os.unlink(f.name)
        assert cfg.conflict_margin == 0.20

    def test_env_overrides_json(self, monkeypatch):
        """Env vars take precedence over JSON file values."""
        data = {"validation_threshold": 0.90}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            monkeypatch.setenv("MEMORYMASTER_VALIDATION_THRESHOLD", "0.99")
            cfg = load_config(config_path=f.name)
        os.unlink(f.name)
        assert cfg.validation_threshold == 0.99

    def test_unknown_keys_ignored(self):
        data = {"nonexistent_key": 42, "validation_threshold": 0.80}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            cfg = load_config(config_path=f.name)
        os.unlink(f.name)
        assert cfg.validation_threshold == 0.80


class TestSingleton:
    def test_get_config_returns_same(self):
        a = get_config()
        b = get_config()
        assert a is b

    def test_set_config(self):
        custom = Config(validation_threshold=0.99)
        set_config(custom)
        assert get_config().validation_threshold == 0.99

    def test_reset_config(self, monkeypatch):
        set_config(Config(validation_threshold=0.99))
        reset_config()
        monkeypatch.setenv("MEMORYMASTER_VALIDATION_THRESHOLD", "0.50")
        assert get_config().validation_threshold == 0.50


class TestConfigImmutability:
    def test_frozen(self):
        cfg = Config()
        with pytest.raises(AttributeError):
            cfg.validation_threshold = 0.99  # type: ignore[misc]
