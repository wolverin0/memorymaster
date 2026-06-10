"""Tests for memorymaster.key_rotator: file parsing, rotation, cooldown, ban."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from memorymaster.key_rotator import (
    KeyRotator,
    _KeySlot,
    _parse_file,
    clear_cache,
    get_rotator,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_cache()
    monkeypatch.delenv("MEMORYMASTER_KEY_FILE", raising=False)


def _make(labels: list[str]) -> KeyRotator:
    return KeyRotator(
        slots=[_KeySlot(label=label, value=f"AIza-{label}") for label in labels],
        cooldown_seconds=0.05,
    )


def test_parse_label_equals_key(tmp_path: Path) -> None:
    p = tmp_path / "keys.env"
    p.write_text(
        "# comment line — ignored\n"
        "fer=AIzaKEY1\n"
        "ivana = AIzaKEY2  \n"
        "\n"
        'quoted="AIzaKEY3"\n',
        encoding="utf-8",
    )
    slots = _parse_file(p)
    assert [(s.label, s.value) for s in slots] == [
        ("fer", "AIzaKEY1"),
        ("ivana", "AIzaKEY2"),
        ("quoted", "AIzaKEY3"),
    ]


def test_parse_bare_keys_get_synthetic_labels(tmp_path: Path) -> None:
    p = tmp_path / "keys.env"
    p.write_text("AIzaA\nAIzaB\n", encoding="utf-8")
    slots = _parse_file(p)
    assert [(s.label, s.value) for s in slots] == [
        ("key1", "AIzaA"),
        ("key2", "AIzaB"),
    ]


def test_parse_deduplicates_by_value(tmp_path: Path) -> None:
    p = tmp_path / "keys.env"
    p.write_text("a=AIzaSAME\nb=AIzaSAME\nc=AIzaOTHER\n", encoding="utf-8")
    slots = _parse_file(p)
    assert [(s.label, s.value) for s in slots] == [
        ("a", "AIzaSAME"),
        ("c", "AIzaOTHER"),
    ]


def test_get_rotator_returns_none_when_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_KEY_FILE", str(tmp_path / "does-not-exist.env"))
    assert get_rotator("gemini") is None


def test_get_rotator_reads_file_and_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "keys.env"
    p.write_text("fer=AIza1\nivana=AIza2\n", encoding="utf-8")
    monkeypatch.setenv("MEMORYMASTER_KEY_FILE", str(p))
    r1 = get_rotator("gemini")
    r2 = get_rotator("gemini")
    assert r1 is r2
    assert len(r1) == 2


def test_round_robin_ordering() -> None:
    r = _make(["a", "b", "c"])
    labels = [r.next_key()[0] for _ in range(6)]
    assert labels == ["a", "b", "c", "a", "b", "c"]


def test_cooldown_is_skipped_until_recovery(monkeypatch) -> None:
    # Drive the rotator's clock directly instead of real sleeps: on Windows
    # CI runners the ~15.6ms timer resolution made sleep(0.06) race the
    # 0.05s cooldown deadline, so "a" sometimes never re-entered rotation.
    # The requirement is cooldown SEMANTICS (excluded until the deadline,
    # eligible after), not wall-clock behavior — a fake clock pins exactly
    # that, deterministically on any machine.
    r = _make(["a", "b", "c"])
    clock = {"t": 1000.0}
    monkeypatch.setattr(type(r), "_now", lambda self: clock["t"])
    # Take a once, put it on cooldown
    assert r.next_key()[0] == "a"
    r.mark_rate_limited("a", retry_after=0.05)
    # Next rotation: b, c, b, c ... (a is on cooldown)
    seen = [r.next_key()[0] for _ in range(4)]
    assert "a" not in seen
    # After the cooldown deadline passes, a re-enters rotation
    clock["t"] += 0.06
    labels = {r.next_key()[0] for _ in range(6)}
    assert "a" in labels


def test_all_on_cooldown_sleeps_and_returns_soonest() -> None:
    r = _make(["a", "b"])
    # Use larger cooldown windows so Windows' ~15.6ms timer resolution can't
    # undershoot the assertion. Previously 0.02/0.10 with a >=0.02 floor was
    # flaky because time.sleep(0.02) often returns in ~0.016s on Windows.
    r.mark_rate_limited("a", retry_after=0.25)
    r.mark_rate_limited("b", retry_after=0.10)
    t0 = time.monotonic()
    label, _ = r.next_key()
    elapsed = time.monotonic() - t0
    assert label == "b"
    # Should have slept ~0.10s (the soonest). Tolerance absorbs clock jitter
    # on low-resolution Windows timers.
    assert elapsed >= 0.08
    assert elapsed < 0.35


def test_banned_keys_are_skipped_permanently() -> None:
    r = _make(["a", "b", "c"])
    r.mark_banned("b", reason="API_KEY_INVALID")
    labels = [r.next_key()[0] for _ in range(10)]
    assert "b" not in labels
    assert set(labels) == {"a", "c"}
    assert len(r) == 2


def test_next_key_returns_none_when_all_banned() -> None:
    r = _make(["a", "b"])
    r.mark_banned("a")
    r.mark_banned("b")
    assert r.next_key() is None
    assert len(r) == 0


def test_retry_after_overrides_default_cooldown() -> None:
    r = _make(["a"])
    r.cooldown_seconds = 999.0  # huge default
    r.mark_rate_limited("a", retry_after=0.05)
    time.sleep(0.06)
    # Should be available again despite the default 999s
    label, _ = r.next_key()
    assert label == "a"


def test_default_cooldown_used_when_no_retry_after() -> None:
    r = _make(["a"])
    r.cooldown_seconds = 0.05
    r.mark_rate_limited("a", retry_after=None)
    # Immediately after: on cooldown (but only one key, so we get it with sleep)
    t0 = time.monotonic()
    r.next_key()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.04
