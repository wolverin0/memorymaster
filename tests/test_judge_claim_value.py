"""Contracts for scripts/judge_claim_value.py (no network: transport is mocked)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from scripts import judge_claim_value as jcv

FAKE_KEY = "test-key-not-real-0001"


def _make_db(path: Path, rows: list[tuple[Any, ...]]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT, claim_type TEXT, subject TEXT, predicate TEXT,"
        " object_value TEXT, scope TEXT, status TEXT, source_agent TEXT, created_at TEXT, visibility TEXT)"
    )
    conn.executemany(
        "INSERT INTO claims (id, text, claim_type, subject, predicate, object_value, scope, status,"
        " source_agent, created_at, visibility) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def _rows(count: int, status: str = "confirmed") -> list[tuple[Any, ...]]:
    return [
        (
            i,
            f"claim number {i} about widgets",
            "fact",
            None,
            None,
            None,
            "project:demo",
            status,
            "claude-session" if i % 2 else "codex-session",
            "2026-10-01T00:00:00",
            "public",
        )
        for i in range(1, count + 1)
    ]


def _claims(count: int) -> list[dict[str, Any]]:
    return [
        {
            "id": i,
            "text": f"claim {i}",
            "scope": "project:demo",
            "claim_type": "fact",
            "source_agent": "claude-session" if i % 2 else "codex-session",
            "created_at": "2026-10-01",
        }
        for i in range(1, count + 1)
    ]


def _ok_body(verdicts: list[dict[str, Any]] | str) -> dict[str, Any]:
    text = verdicts if isinstance(verdicts, str) else json.dumps(verdicts)
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _all_useful(batch_size: int) -> list[dict[str, Any]]:
    return [{"n": n, "verdict": "useful", "reason": "ok"} for n in range(1, batch_size + 1)]


def test_sample_is_seed_deterministic_and_confirmed_only(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path / "src.db",
        _rows(60)
        + [(100 + i, "stale one", "fact", None, None, None, "s", "stale", "a", "t", "public") for i in range(20)],
    )
    first = jcv.sample_claims(db, 15, seed=7)
    second = jcv.sample_claims(db, 15, seed=7)
    other = jcv.sample_claims(db, 15, seed=8)
    assert [c["id"] for c in first] == [c["id"] for c in second]
    assert [c["id"] for c in first] != [c["id"] for c in other]
    assert len(first) == 15 and all(c["id"] <= 60 for c in first)


def test_snapshot_uses_backup_and_leaves_source_untouched(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "src.db", _rows(5))
    before = db.read_bytes()
    snap = jcv.snapshot_db(db, tmp_path / "out" / "snapshot.db")
    assert db.read_bytes() == before
    conn = sqlite3.connect(snap)
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 5
    conn.close()


def test_sensitivity_filter_drops_flagged_and_sensitive_visibility() -> None:
    secret = "GEMINI_API_KEY=AIza" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r"
    claims = [
        {"id": 1, "text": "plain architecture decision about retries", "visibility": "public"},
        {"id": 2, "text": f"deploy notes {secret}", "visibility": "public"},
        {"id": 3, "text": "innocuous text", "visibility": "sensitive"},
        {"id": 4, "text": "already [REDACTED:token] in text", "visibility": "public"},
    ]
    kept, dropped = jcv.filter_sensitive(claims)
    assert [c["id"] for c in kept] == [1]
    assert dropped == 3


def test_outgoing_text_masks_home_paths_and_private_ips() -> None:
    masked = jcv.mask_outgoing(
        r"see C:\Users\someone\proj and /home/bob/x on 192.168.1.20 and 10.0.0.5 but 8.8.8.8 stays"
    )
    assert "someone" not in masked and "bob" not in masked
    assert "192.168.1.20" not in masked and "10.0.0.5" not in masked
    assert "8.8.8.8" in masked


def test_parse_verdicts_accepts_valid_fenced_and_wrapped() -> None:
    raw = '[{"n":1,"verdict":"useful","reason":"a"},{"n":2,"verdict":"JUNK","reason":"b"}]'
    assert jcv.parse_verdicts(raw, 2) == {1: ("useful", "a"), 2: ("junk", "b")}
    assert jcv.parse_verdicts("```json\n" + raw + "\n```", 2)[2][0] == "junk"
    assert jcv.parse_verdicts("Here you go: " + raw + " done", 2)[1][0] == "useful"
    assert jcv.parse_verdicts(json.dumps({"verdicts": json.loads(raw)}), 2)[1][0] == "useful"


@pytest.mark.parametrize(
    "raw",
    ["", "not json at all", '[{"n":1,"verdict":"useful"', '{"n":1}', "[1,2,3]", '[{"n":"x","verdict":"useful"}]'],
)
def test_parse_verdicts_malformed_returns_empty(raw: str) -> None:
    assert jcv.parse_verdicts(raw, 3) == {}


def test_parse_verdicts_drops_bad_entries_but_keeps_good_ones() -> None:
    raw = json.dumps(
        [
            {"n": 1, "verdict": "useful", "reason": "ok"},
            {"n": 2, "verdict": "great", "reason": "invalid verdict"},
            {"n": 9, "verdict": "junk", "reason": "out of range"},
            "garbage",
        ]
    )
    assert jcv.parse_verdicts(raw, 3) == {1: ("useful", "ok")}


def test_request_cap_is_hard_including_retries() -> None:
    calls: list[int] = []

    def transport(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, dict]:
        calls.append(1)
        return 429, {}

    judge = jcv.Judge([FAKE_KEY], max_requests=3, transport=transport, sleep=lambda _s: None)
    assert judge.judge_batch(_claims(5)) is None
    assert len(calls) == 3 and judge.requests == 3


def test_cap_stops_judge_all_and_reports_unjudged() -> None:
    calls: list[int] = []

    def transport(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, dict]:
        calls.append(1)
        return 200, _ok_body(_all_useful(5))

    judge = jcv.Judge([FAKE_KEY], max_requests=2, transport=transport, sleep=lambda _s: None)
    seen: list[int] = []
    stats = jcv.judge_all(_claims(20), judge, batch_size=5, on_verdict=lambda c, v, r: seen.append(c["id"]))
    assert len(calls) == 2 and len(seen) == 10
    assert stats == {"unjudged": 10, "requests": 2}


def test_backoff_retries_5xx_then_succeeds_and_key_stays_out_of_url() -> None:
    sleeps: list[float] = []
    seen: list[tuple[str, dict]] = []
    replies = iter([(503, {}), (429, {}), (200, _ok_body(_all_useful(2)))])

    def transport(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, dict]:
        seen.append((url, headers))
        return next(replies)

    judge = jcv.Judge([FAKE_KEY], max_requests=10, transport=transport, sleep=sleeps.append)
    result = judge.judge_batch(_claims(2))
    assert result is not None and set(result) == {1, 2}
    assert sleeps == [2.0, 4.0]
    assert all(FAKE_KEY not in url for url, _ in seen)
    assert judge.requests == 3


def test_non_retryable_status_raises_without_leaking_key() -> None:
    judge = jcv.Judge([FAKE_KEY], max_requests=5, transport=lambda *a: (400, {}), sleep=lambda _s: None)
    with pytest.raises(jcv.JudgeError) as info:
        judge.judge_batch(_claims(1))
    assert FAKE_KEY not in str(info.value)


def test_malformed_reply_is_retried_then_claims_stay_unjudged() -> None:
    calls: list[int] = []

    def transport(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, dict]:
        calls.append(1)
        return 200, _ok_body("this is not json")

    judge = jcv.Judge([FAKE_KEY], max_requests=40, transport=transport, sleep=lambda _s: None)
    seen: list[int] = []
    stats = jcv.judge_all(_claims(4), judge, batch_size=4, on_verdict=lambda c, v, r: seen.append(c["id"]))
    assert seen == []
    assert len(calls) == jcv.MAX_ATTEMPTS_PER_CLAIM  # bounded retries, no infinite loop
    assert stats["unjudged"] == 4


def test_partial_reply_retries_only_missing_claims() -> None:
    prompts: list[str] = []
    replies = iter([_ok_body([{"n": 1, "verdict": "junk", "reason": "x"}]), _ok_body(_all_useful(2))])

    def transport(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, dict]:
        prompts.append(payload["contents"][0]["parts"][0]["text"])
        return 200, next(replies)

    judge = jcv.Judge([FAKE_KEY], max_requests=40, transport=transport, sleep=lambda _s: None)
    got: dict[int, str] = {}
    stats = jcv.judge_all(_claims(3), judge, batch_size=3, on_verdict=lambda c, v, r: got.__setitem__(c["id"], v))
    assert got == {1: "junk", 2: "useful", 3: "useful"}
    assert stats["unjudged"] == 0 and len(prompts) == 2


def test_summary_ranks_agents_by_bad_share_with_min_n() -> None:
    rows = (
        [{"id": i, "scope": "a", "claim_type": "fact", "source_agent": "good", "verdict": "useful"} for i in range(10)]
        + [
            {
                "id": 100 + i,
                "scope": "b",
                "claim_type": "fact",
                "source_agent": "bad",
                "verdict": "junk" if i < 6 else "useful",
            }
            for i in range(10)
        ]
        + [
            {"id": 200 + i, "scope": "b", "claim_type": "fact", "source_agent": "tiny", "verdict": "junk"}
            for i in range(3)
        ]
    )
    summary = jcv.build_summary(rows, {}, human={"0": "useful", "100": "useful"})
    worst = [g["name"] for g in summary["worst_source_agents"]]
    assert worst == ["bad", "good"]  # "tiny" (n=3) is not ranked
    assert summary["overall"]["counts"]["junk"] == 9
    assert summary["human_agreement"]["exact"] == 1 and summary["human_agreement"]["n"] == 2


def test_end_to_end_run_writes_outputs_without_touching_source(tmp_path: Path) -> None:
    secret_row = (
        99,
        "token AIza" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r",
        "fact",
        None,
        None,
        None,
        "s",
        "confirmed",
        "a",
        "2026-10-01",
        "public",
    )
    db = _make_db(tmp_path / "src.db", _rows(30) + [secret_row])
    before = db.read_bytes()
    out = tmp_path / "out"
    sent: list[str] = []

    def transport(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, dict]:
        sent.append(payload["contents"][0]["parts"][0]["text"])
        size = payload["contents"][0]["parts"][0]["text"].count('"n":')
        return 200, _ok_body(_all_useful(size))

    args = jcv.build_parser().parse_args(
        [
            "--n",
            "31",
            "--seed",
            "1",
            "--db",
            str(db),
            "--out-dir",
            str(out),
            "--batch",
            "10",
            "--model",
            "m",
            "--min-interval",
            "0",
        ]
    )
    import os

    os.environ["GEMINI_API_KEY"] = FAKE_KEY
    try:
        summary = jcv.run(args, transport=transport, sleep=lambda _s: None)
    finally:
        del os.environ["GEMINI_API_KEY"]
    assert db.read_bytes() == before
    assert summary["dropped_sensitive"] == 1 and summary["judged"] == 30
    assert all("AIza" not in prompt for prompt in sent)
    assert not (out / "snapshot.db").exists()
    assert len(jcv.read_jsonl(out / "verdicts.jsonl")) == 30
    again = jcv.run(jcv.build_parser().parse_args(["--out-dir", str(out), "--report"]))
    assert again["judged"] == 30 and again["dropped_sensitive"] == 1


def test_dry_run_makes_no_requests(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "src.db", _rows(10))

    def transport(*a: Any) -> tuple[int, dict]:
        raise AssertionError("dry run must not hit the network")

    args = jcv.build_parser().parse_args(
        [
            "--n",
            "5",
            "--seed",
            "1",
            "--db",
            str(db),
            "--out-dir",
            str(tmp_path / "o"),
            "--batch",
            "5",
            "--model",
            "m",
            "--dry-run",
        ]
    )
    summary = jcv.run(args, transport=transport)
    assert summary["sampled"] == 5 and summary["dry_run"] is True


def test_real_run_reuses_frozen_sample_so_blind_labels_stay_in_the_judged_set(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "src.db", _rows(40))
    out = tmp_path / "o"
    base = [
        "--n",
        "10",
        "--seed",
        "3",
        "--db",
        str(db),
        "--out-dir",
        str(out),
        "--batch",
        "5",
        "--model",
        "m",
        "--min-interval",
        "0",
    ]
    dry = jcv.run(jcv.build_parser().parse_args([*base, "--dry-run"]))
    frozen_ids = [c["id"] for c in jcv.read_jsonl(out / "sample.jsonl")]

    # The live DB drifts after the dry run (decay): every sampled claim goes stale and new ones appear.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE claims SET status='stale'")
    conn.executemany(
        "INSERT INTO claims (id, text, claim_type, scope, status, source_agent, created_at, visibility) VALUES (?,?,?,?,?,?,?,?)",
        [(500 + i, f"new {i}", "fact", "s", "confirmed", "a", "2026-10-02", "public") for i in range(30)],
    )
    conn.commit()
    conn.close()

    def transport(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, dict]:
        return 200, _ok_body(_all_useful(payload["contents"][0]["parts"][0]["text"].count('"n":')))

    import os

    os.environ["GEMINI_API_KEY"] = FAKE_KEY
    try:
        summary = jcv.run(jcv.build_parser().parse_args(base), transport=transport, sleep=lambda _s: None)
    finally:
        del os.environ["GEMINI_API_KEY"]
    assert summary["sample_reused"] is True and summary["sampled"] == dry["sampled"]
    assert sorted(r["id"] for r in jcv.read_jsonl(out / "verdicts.jsonl")) == sorted(frozen_ids)


def test_thinking_config_matches_model_family() -> None:
    assert jcv.build_payload(_claims(1), "gemini-2.5-flash")["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 0
    }
    assert jcv.build_payload(_claims(1), "gemini-3.5-flash")["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "minimal"
    }
    assert jcv.build_payload(_claims(1))["generationConfig"]["temperature"] == 0
