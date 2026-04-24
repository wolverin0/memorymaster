"""Tests for the wiki freshness metric (roadmap item 11.8 — Option A)."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.wiki_freshness import (
    ArticleFreshness,
    FRESHNESS_SCALE_DAYS,
    STALE_ARTICLE_THRESHOLD,
    bucket_distribution,
    freshness_for_article,
    scan_vault,
)


def _write_article(
    vault: Path,
    scope: str,
    slug: str,
    *,
    date: str | None,
    title: str | None = None,
    mtime: datetime | None = None,
) -> Path:
    """Write a wiki article with optional frontmatter ``date:`` and mtime."""
    scope_dir = vault / scope
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / f"{slug}.md"
    lines: list[str] = []
    if date is not None:
        lines.append("---")
        lines.append(f"title: {title or slug.replace('-', ' ').title()}")
        lines.append(f"scope: {scope}")
        lines.append(f"date: {date}")
        lines.append("---")
        lines.append("")
    lines.append(f"# {title or slug}")
    lines.append("")
    lines.append("Body content.")
    path.write_text("\n".join(lines), encoding="utf-8")
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))
    return path


def _fixture_vault(tmp_path: Path, now: datetime) -> Path:
    vault = tmp_path / "wiki"
    vault.mkdir()
    today = now.strftime("%Y-%m-%d")
    mid = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    stale = (now - timedelta(days=120)).strftime("%Y-%m-%d")
    _write_article(vault, "project-test", "fresh-article", date=today, title="Fresh")
    _write_article(vault, "project-test", "mid-article", date=mid, title="Mid")
    _write_article(vault, "project-test", "stale-article", date=stale, title="Stale")
    return vault


def test_scan_vault_returns_articles_sorted_stalest_first(tmp_path):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    vault = _fixture_vault(tmp_path, now)
    snapshots = scan_vault(vault, now=now)

    assert len(snapshots) == 3
    # Sorted lowest-score first.
    assert snapshots[0].title == "Stale"
    assert snapshots[-1].title == "Fresh"


def test_scan_vault_scores_match_exponential_decay(tmp_path):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    vault = _fixture_vault(tmp_path, now)
    by_title = {s.title: s for s in scan_vault(vault, now=now)}

    expected_fresh = math.exp(0 / FRESHNESS_SCALE_DAYS)  # 1.0
    expected_mid = math.exp(-30 / FRESHNESS_SCALE_DAYS)  # ~0.3679
    expected_stale = math.exp(-120 / FRESHNESS_SCALE_DAYS)  # ~0.0183

    # ±1% tolerance allows date-boundary rounding (we store as YYYY-MM-DD).
    assert by_title["Fresh"].freshness_score == pytest.approx(expected_fresh, rel=0.01)
    assert by_title["Mid"].freshness_score == pytest.approx(expected_mid, rel=0.01)
    assert by_title["Stale"].freshness_score == pytest.approx(expected_stale, rel=0.01)


def test_frontmatter_date_beats_mtime(tmp_path):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    vault = tmp_path / "wiki"
    vault.mkdir()
    # Frontmatter says article was absorbed today; mtime is 200 days old.
    old_mtime = now - timedelta(days=200)
    _write_article(
        vault,
        "scope-a",
        "today",
        date=now.strftime("%Y-%m-%d"),
        mtime=old_mtime,
    )
    snap = freshness_for_article(vault / "scope-a" / "today.md", now=now)
    assert snap is not None
    assert snap.days_since_absorb < 1.5
    assert snap.freshness_score == pytest.approx(1.0, rel=0.02)


def test_no_frontmatter_falls_back_to_mtime(tmp_path):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    vault = tmp_path / "wiki"
    vault.mkdir()
    # No frontmatter at all.
    path = _write_article(
        vault,
        "scope-a",
        "legacy",
        date=None,
        mtime=now - timedelta(days=45),
    )
    snap = freshness_for_article(path, now=now)
    assert snap is not None
    assert 40 < snap.days_since_absorb < 50
    assert snap.freshness_score == pytest.approx(math.exp(-45 / 30), rel=0.05)


def test_unparseable_frontmatter_date_falls_back_to_mtime(tmp_path):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    vault = tmp_path / "wiki"
    vault.mkdir()
    scope_dir = vault / "scope-a"
    scope_dir.mkdir()
    path = scope_dir / "bad-date.md"
    path.write_text(
        "---\ntitle: Bad\ndate: not-a-date\n---\n\nbody",
        encoding="utf-8",
    )
    old = (now - timedelta(days=10)).timestamp()
    os.utime(path, (old, old))
    snap = freshness_for_article(path, now=now)
    assert snap is not None
    assert 8 < snap.days_since_absorb < 12


def test_bucket_distribution_counts(tmp_path):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    vault = _fixture_vault(tmp_path, now)
    snapshots = scan_vault(vault, now=now)
    dist = bucket_distribution(snapshots)
    assert dist == {"fresh": 1, "mid": 1, "stale": 1}


def test_scan_vault_skips_bases_and_underscore_files(tmp_path):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    vault = tmp_path / "wiki"
    (vault / "project-x").mkdir(parents=True)
    (vault / "bases").mkdir()
    # Under-bases and _index-style files must be excluded.
    (vault / "bases" / "all.md").write_text("ignore", encoding="utf-8")
    (vault / "project-x" / "_index.md").write_text("ignore", encoding="utf-8")
    _write_article(
        vault,
        "project-x",
        "real",
        date=now.strftime("%Y-%m-%d"),
    )
    snapshots = scan_vault(vault, now=now)
    assert [s.path.name for s in snapshots] == ["real.md"]


def test_stale_threshold_constant_is_reasonable():
    # ~48 days is where the curve crosses 0.2 (exp(-48/30) ~= 0.202).
    # Lock the threshold so future tweaks surface in tests rather than silently
    # shifting lint behavior.
    assert STALE_ARTICLE_THRESHOLD == pytest.approx(0.2, rel=0.0001)
    # Sanity check: a 60-day-old article should comfortably score below the
    # stale threshold, a 30-day-old one should stay above it.
    assert math.exp(-60 / FRESHNESS_SCALE_DAYS) < STALE_ARTICLE_THRESHOLD
    assert math.exp(-30 / FRESHNESS_SCALE_DAYS) > STALE_ARTICLE_THRESHOLD


def test_vault_linter_flags_stale_articles(tmp_path):
    """lint-vault should emit a stale_articles list sourced from the wiki."""
    from memorymaster.vault_linter import _detect_stale_articles

    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    vault = _fixture_vault(tmp_path, now)
    stale = _detect_stale_articles(vault)
    titles = {item["title"] for item in stale}
    assert "Stale" in titles
    # Fresh / Mid should NOT be flagged.
    assert "Fresh" not in titles
    assert "Mid" not in titles


def test_cli_wiki_freshness_json_smoke(tmp_path):
    """Smoke test: the CLI subcommand runs and emits a parseable JSON envelope."""
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    vault = _fixture_vault(tmp_path, now)

    # Touch the freshly-written files so their mtime reflects "now" — we rely
    # on frontmatter dates anyway, but keep the env clean.
    for md in vault.rglob("*.md"):
        ts = time.time()
        os.utime(md, (ts, ts))

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "memorymaster",
            "--json",
            "--db",
            str(tmp_path / "does-not-exist.db"),
            "wiki-freshness",
            "--vault",
            str(vault),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"

    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["total_articles"] == 3
    assert "distribution" in data
    assert set(data["distribution"].keys()) == {"fresh", "mid", "stale"}
    assert len(data["articles"]) == 3
    article = data["articles"][0]
    assert {"path", "title", "scope", "days_since_absorb", "freshness_score"} <= article.keys()


def test_cli_wiki_freshness_below_filter(tmp_path):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    vault = _fixture_vault(tmp_path, now)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "memorymaster",
            "--json",
            "--db",
            str(tmp_path / "x.db"),
            "wiki-freshness",
            "--vault",
            str(vault),
            "--below",
            "0.4",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    titles = {a["title"] for a in payload["data"]["articles"]}
    # Fresh should be excluded, Mid and Stale should remain.
    assert "Fresh" not in titles
    assert "Stale" in titles


def test_article_freshness_dataclass_is_frozen():
    snap = ArticleFreshness(
        path=Path("fake"),
        title="T",
        scope="s",
        days_since_absorb=0.0,
        freshness_score=1.0,
    )
    with pytest.raises(Exception):
        snap.title = "mutated"  # type: ignore[misc]
