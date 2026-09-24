"""scripts/eval_graph_recall.py: frozen cohort, read-only replay, honest metrics."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.feedback import FeedbackTracker
from scripts import eval_graph_recall

KEPT = {
    "where does qdrant keep the recall vectors?",
    "how do the recall vectors depend on qdrant?",
    "who owns the storage box?",
    "what changed in release 4.8.9?",
}
PHONE_TEXTS = (
    "can you text +54 9 2477 31-5837 about the router?",
    "did 5492477660092 answer the ticket?",
)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path_factory):
    monkeypatch.delenv("MEMORYMASTER_RECALL_GRAPH_MODE", raising=False)
    monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv(
        "MEMORYMASTER_DECISIONS_DB",
        str(tmp_path_factory.mktemp("decisions") / "decisions.db"),
    )


def _repo(tmp_path: Path) -> Path:
    """A nested git repo whose ``private/`` dir is ignored, like ``artifacts/``."""
    if shutil.which("git") is None:
        pytest.skip("git is required to prove the texts file is git-ignored")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("private/\n", encoding="utf-8")
    return repo


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "eval.db"
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()
    seed = svc.ingest("qdrant stores the recall vectors", [CitationInput(source="t://a")],
                      scope="project:x")
    leaf = svc.ingest("the storage box snapshots nightly", [CitationInput(source="t://b")],
                      scope="project:x")
    for claim in (seed, leaf):
        svc.store.apply_status_transition(claim, to_status="confirmed", reason="f",
                                          event_type="transition")
    svc.add_claim_link(seed.id, leaf.id, "relates_to")
    FeedbackTracker(str(db)).ensure_tables()
    prompts = [
        *sorted(KEPT),
        *PHONE_TEXTS,
        "can you mail ops@example.com about it?",
        "what is in C:/tmp/secret.txt?",
        "labelled prompt from the old eval?",
        "this is not a question",
    ]
    conn = sqlite3.connect(db)
    try:
        for i, text in enumerate(prompts):
            conn.execute(
                "INSERT INTO usage_feedback (id, claim_id, query_text, timestamp, was_returned)"
                " VALUES (?, ?, ?, '2026-09-23T00:00:00+00:00', 1)",
                (f"f{i}", seed.id, text),
            )
        conn.commit()
        # Fold the WAL into the main file so a later digest sees only reader effects.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return db


def _digest(path: Path) -> str:
    wal = path.with_name(path.name + "-wal")
    data = path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
    return hashlib.sha256(data).hexdigest()


def _flip(klass: str) -> str:
    return "relational" if klass == "ordinary" else "ordinary"


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("send to +54 9 2477 31-5837 or 5492477660092 ask oscar", True),
        ("did 5492477660092 answer?", True),
        ("call (011) 4555-1234 tomorrow?", True),
        ("is ticket 4471234 closed?", True),
        ("what changed in release 4.8.9?", False),
        ("top 5 claims of 2026?", False),
        ("is 1+1 still 2?", False),
    ],
)
def test_phone_numbers_and_long_digit_runs_are_flagged(text, flagged):
    assert eval_graph_recall.has_phone_or_digit_run(text) is flagged


def test_cohort_is_frozen_private_and_excludes_labelled_prompts(tmp_path):
    db = _db(tmp_path)
    repo = _repo(tmp_path)
    excluded = tmp_path / "labelled.jsonl"
    excluded.write_text(json.dumps({"text": "labelled prompt from the old eval?"}) + "\n",
                        encoding="utf-8")
    manifest = repo / "evidence" / "cohort.json"
    texts = repo / "private" / "cohort.texts.json"
    before = _digest(db)
    cohort = eval_graph_recall.build_cohort(
        db, manifest, texts_out=texts, per_class=10, seed="s", exclude_prompts=[excluded]
    )
    assert _digest(db) == before
    loaded = eval_graph_recall._load_cohort(manifest, texts)
    assert {q["text"] for q in loaded["questions"]} == KEPT
    assert {q["class"] for q in loaded["questions"] if "depend" in q["text"]} == {"relational"}
    counters = cohort["counters"]
    assert counters["dropped_phone_or_digit_run"] == 2
    assert counters["dropped_private_or_redactable"] == 1
    assert counters["dropped_local_path"] == 1
    assert counters["dropped_953_labelled"] == 1
    assert counters["dropped_not_question"] == 1

    # The manifest (the file that may be committed) carries ids, classes and
    # hashes only -- no query text, kept or dropped.
    raw_manifest = manifest.read_text(encoding="utf-8")
    for text in (*KEPT, *PHONE_TEXTS):
        assert text not in raw_manifest
    assert "5837" not in raw_manifest and "5492477660092" not in raw_manifest
    stored = json.loads(raw_manifest)
    assert all(set(q) == {"id", "class", "text_sha256"} for q in stored["questions"])
    assert stored["texts_file"] == texts.name  # a name, never a local path

    # Tampering with either half of the frozen cohort is detected.
    original_texts = texts.read_text(encoding="utf-8")
    data = json.loads(original_texts)
    first = next(iter(data["texts"]))
    data["texts"][first] = "tampered?"
    texts.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SystemExit, match="sha256"):
        eval_graph_recall._load_cohort(manifest, texts)
    texts.write_text(original_texts, encoding="utf-8")
    stored["questions"][0]["class"] = _flip(stored["questions"][0]["class"])
    manifest.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(SystemExit, match="sha256"):
        eval_graph_recall._load_cohort(manifest, texts)


def test_cohort_texts_are_refused_outside_a_git_ignored_path(tmp_path):
    db = _db(tmp_path)
    repo = _repo(tmp_path)
    manifest = repo / "evidence" / "cohort.json"
    tracked = repo / "evidence" / "cohort.texts.json"
    with pytest.raises(SystemExit, match="git-ignored"):
        eval_graph_recall.build_cohort(db, manifest, texts_out=tracked, per_class=10, seed="s")
    assert not tracked.exists()
    assert not manifest.exists()


def test_run_is_read_only_and_marks_unlabelled_metrics_unmeasured(tmp_path):
    db = _db(tmp_path)
    repo = _repo(tmp_path)
    cohort = repo / "evidence" / "cohort.json"
    texts = repo / "private" / "cohort.texts.json"
    eval_graph_recall.build_cohort(db, cohort, texts_out=texts, per_class=10, seed="s")
    before = _digest(db)
    out = repo / "evidence" / "run"
    summary = eval_graph_recall.run(db, cohort, out, texts=texts, limit=5, modes=["legacy"])
    assert _digest(db) == before
    assert summary["oracle"]["trustworthy"] is False
    assert summary["oracle"]["coverage"] == 0.0
    arms = summary["arms"]
    assert set(arms) == {"legacy/baseline", "legacy/vector_first", "legacy/graph_first"}
    for arm in arms.values():
        assert arm["hit_at_5"] == "UNMEASURED"
        assert arm["precision_at_5"] == "UNMEASURED"
        assert arm["retired_support_exposed"] == 0
    assert arms["legacy/vector_first"]["graph_rows_total"] >= 1
    assert arms["legacy/vector_first"]["supported_path_rate"] == 1.0
    assert arms["legacy/baseline"]["graph_rows_total"] == 0
    evidence = "".join(p.read_text(encoding="utf-8") for p in sorted(out.iterdir()))
    assert "storage box snapshots" not in evidence  # ids only, never claim text
    for text in KEPT:
        assert text not in evidence  # nor query text

    # Latent reach (why expansion can or cannot add anything) is produced by
    # the script itself from independent SQL, for the baseline arm only.
    reach = arms["legacy/baseline"]["latent_reach"]
    assert reach["questions_with_any_edge_from_base"] >= 1
    assert reach["questions_with_confirmed_to_confirmed_edge"] >= 1
    assert reach["neighbour_status"] == {"confirmed": reach["questions_with_any_edge_from_base"]}
    assert "latent_reach" not in arms["legacy/vector_first"]


def _claim_id(db: Path, text: str) -> int:
    with sqlite3.connect(db) as conn:
        return int(conn.execute("SELECT id FROM claims WHERE text = ?", (text,)).fetchone()[0])


def test_run_measures_ranking_only_with_enough_labels(tmp_path):
    db = _db(tmp_path)
    repo = _repo(tmp_path)
    cohort = repo / "evidence" / "cohort.json"
    texts = repo / "private" / "cohort.texts.json"
    eval_graph_recall.build_cohort(db, cohort, texts_out=texts, per_class=10, seed="s")
    questions = eval_graph_recall._load_cohort(cohort, texts)["questions"]
    qid = next(q["id"] for q in questions if q["text"].startswith("where does qdrant"))
    labels = repo / "evidence" / "labels.json"
    labels.write_text(json.dumps({"labels": {
        qid: [_claim_id(db, "qdrant stores the recall vectors")],
        "not-in-cohort": [1],
    }}), encoding="utf-8")

    measured = eval_graph_recall.run(db, cohort, repo / "evidence" / "m", texts=texts,
                                     limit=5, modes=["legacy"], labels=labels, min_labelled=1)
    oracle = measured["oracle"]
    assert oracle["labelled_questions"] == 1
    assert oracle["labels_unknown_ids"] == 1
    assert oracle["coverage"] == pytest.approx(1 / len(questions), abs=1e-4)
    baseline = measured["arms"]["legacy/baseline"]
    assert baseline["labelled_questions"] == 1
    assert baseline["hit_at_5"] == 1.0
    assert baseline["precision_at_5"] == pytest.approx(0.2)

    # Too few labelled questions: coverage is still reported, ranking is not.
    thin = eval_graph_recall.run(db, cohort, repo / "evidence" / "t", texts=texts,
                                 limit=5, modes=["legacy"], labels=labels, min_labelled=30)
    assert thin["oracle"]["coverage"] == oracle["coverage"]
    for arm in thin["arms"].values():
        assert arm["hit_at_5"] == "UNMEASURED"
        assert arm["precision_at_5"] == "UNMEASURED"
