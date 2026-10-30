"""T-1875: recall self-check by rehearsal (temp DBs, stub question generator, no network)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import selfcheck
from memorymaster.surfaces.cli import main as cli_main

TEXTS = {
    "quokka": ("The quokka rollup job holds the nightly lock for forty minutes", "project:alpha"),
    "zebra": ("The zebra exporter writes parquet files into the cold bucket", "project:alpha"),
    "lemur": ("The lemur gateway rejects tokens older than one hour", "project:beta"),
}


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "claims.db"
    svc = MemoryService(str(path), workspace_root=tmp_path)
    svc.init_db()
    for text, scope in TEXTS.values():
        claim = svc.ingest(text, [CitationInput(source="test")], scope=scope, source_agent="test")
        transition_claim(svc.store, claim.id, "confirmed", reason="fixture", event_type="validator")
    return path


def _fingerprint(path: Path) -> str:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT id, status, access_count, updated_at, last_accessed FROM claims ORDER BY id").fetchall()
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()
    return hashlib.sha256(json.dumps([rows, events], default=str).encode()).hexdigest()


def stub(seen: list[str] | None = None):
    """Questions that share a keyword with their claim, except lemur (asks about something else)."""
    def generate(claim_text: str, n: int) -> list[str]:
        if seen is not None:
            seen.append(claim_text)
        if "lemur" in claim_text:
            return ["What does the platypus billing service do?"]
        word = "quokka" if "quokka" in claim_text else "zebra"
        return [f"how long does {word} hold the lock", f"{word} job details"][:n]
    return generate


def test_hit_rate_per_scope_and_unrecoverable_list(db: Path, tmp_path: Path) -> None:
    sidecar = tmp_path / "side.sqlite"
    run = selfcheck.run_selfcheck(db, sidecar_path=sidecar, generate=stub())
    assert run.status == "ok"
    assert (run.claims_evaluated, run.claims_hit) == (3, 2)
    assert run.hit_rate == pytest.approx(2 / 3, abs=1e-3)
    assert [m["scope"] for m in run.unrecoverable] == ["project:beta"]

    report = selfcheck.build_report(sidecar)
    assert report["scopes"]["project:alpha"] == {"evaluated": 2, "hit": 2, "hit_rate": 1.0}
    assert report["scopes"]["project:beta"] == {"evaluated": 1, "hit": 0, "hit_rate": 0.0}
    assert report["total"]["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert [m["scope"] for m in report["unrecoverable"]] == ["project:beta"]
    assert "lemur" in report["unrecoverable"][0]["excerpt"]
    assert selfcheck.build_report(sidecar, scope="project:alpha")["unrecoverable"] == []


def test_claims_db_is_untouched(db: Path, tmp_path: Path) -> None:
    before = _fingerprint(db)
    selfcheck.run_selfcheck(db, sidecar_path=tmp_path / "side.sqlite", generate=stub())
    assert _fingerprint(db) == before  # no claims written, no access recorded by the rehearsal


def test_second_run_skips_already_checked_claims(db: Path, tmp_path: Path) -> None:
    sidecar = tmp_path / "side.sqlite"
    selfcheck.run_selfcheck(db, sidecar_path=sidecar, generate=stub())
    again = selfcheck.run_selfcheck(db, sidecar_path=sidecar, generate=stub())
    assert again.status == "no_candidates" and again.llm_calls == 0


def test_sensitive_looking_claim_never_reaches_the_provider(db: Path, tmp_path: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute("UPDATE claims SET text = ? WHERE text LIKE '%zebra%'", (r"zebra writes under /home/someone/exports",))
    conn.commit()
    conn.close()
    seen: list[str] = []
    sidecar = tmp_path / "side.sqlite"
    run = selfcheck.run_selfcheck(db, sidecar_path=sidecar, generate=stub(seen))
    assert run.claims_skipped_sensitive == 1 and run.claims_evaluated == 2
    assert not any("zebra" in s or "someone" in s for s in seen)
    assert selfcheck.build_report(sidecar)["skipped_sensitive"] == 1
    # the skipped claim is not counted in the rate and is not retried
    assert selfcheck.run_selfcheck(db, sidecar_path=sidecar, generate=stub(seen)).status == "no_candidates"


def test_provider_returning_nothing_aborts_fail_closed(db: Path, tmp_path: Path) -> None:
    calls: list[str] = []

    def dead(claim_text: str, n: int) -> list[str]:
        calls.append(claim_text)
        return []

    sidecar = tmp_path / "side.sqlite"
    run = selfcheck.run_selfcheck(db, sidecar_path=sidecar, generate=dead)
    assert run.status == "provider_unavailable"
    assert len(calls) == selfcheck.CONSECUTIVE_FAILURE_LIMIT < len(TEXTS)
    assert selfcheck.build_report(sidecar)["status"] == "empty"  # nothing recorded: a retry is possible


def test_call_budget_caps_the_run(db: Path, tmp_path: Path) -> None:
    run = selfcheck.run_selfcheck(db, sidecar_path=tmp_path / "s.sqlite", generate=stub(), max_calls=1)
    assert run.status == "budget_exhausted" and run.reason == "calls"
    assert run.llm_calls == 1 and run.claims_evaluated == 1


def test_token_budget_caps_the_run(db: Path, tmp_path: Path) -> None:
    run = selfcheck.run_selfcheck(db, sidecar_path=tmp_path / "s.sqlite", generate=stub(), max_tokens=10)
    assert run.status == "budget_exhausted" and run.reason == "tokens" and run.llm_calls == 0


def test_questions_with_secrets_are_dropped(db: Path, tmp_path: Path) -> None:
    def leaky(claim_text: str, n: int) -> list[str]:
        return [r"look in /home/someone/notes", "quokka rollup lock"]

    sidecar = tmp_path / "s.sqlite"
    selfcheck.run_selfcheck(db, sidecar_path=sidecar, generate=leaky, scope="project:alpha", max_claims=1)
    stored = sqlite3.connect(sidecar).execute("SELECT questions_json FROM selfcheck_results").fetchone()[0]
    assert "someone" not in stored and "rollup lock" in stored


def test_default_generator_parses_provider_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import memorymaster.core.llm_provider as lp

    monkeypatch.setattr(lp, "call_llm", lambda prompt, text: '```json\n["a?", "b?", 3]\n```')
    assert selfcheck.llm_question_generator("fact", 3) == ["a?", "b?"]
    monkeypatch.setattr(lp, "call_llm", lambda prompt, text: "")
    assert selfcheck.llm_question_generator("fact", 3) == []


def test_qdrant_mode_and_missing_db_are_rejected(db: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        selfcheck.run_selfcheck(db, sidecar_path=tmp_path / "s.sqlite", generate=stub(), retrieval_mode="qdrant")
    with pytest.raises(FileNotFoundError):
        selfcheck.run_selfcheck(tmp_path / "nope.db", generate=stub())


def test_cli_run_and_report(db: Path, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfcheck, "llm_question_generator", stub())
    side = str(tmp_path / "cli.sqlite")
    assert cli_main(["--db", str(db), "recall-selfcheck", "run", "--sidecar", side]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["claims_evaluated"] == 3 and out["claims_hit"] == 2
    assert cli_main(["--db", str(db), "recall-selfcheck", "report", "--sidecar", side]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["scopes"]["project:beta"]["hit_rate"] == 0.0
    assert not Path(str(db) + ".recall-selfcheck.sqlite").exists()  # explicit --sidecar honored


def test_cli_without_provider_fails_closed(db: Path, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "no-such-provider")
    monkeypatch.delenv("MEMORYMASTER_LLM_FALLBACK_PROVIDER", raising=False)
    rc = cli_main(["--db", str(db), "recall-selfcheck", "run", "--sidecar", str(tmp_path / "x.sqlite")])
    out = json.loads(capsys.readouterr().out)
    assert rc == 3 and out["status"] == "provider_unavailable" and out["claims_evaluated"] == 0
