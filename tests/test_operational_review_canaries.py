"""F-12: the operational review accepts several retrieval canaries.

Review F-12: a single canary sat at rank 5 of 5 -- any new competing claim
evicts it, and one boundary canary is weak evidence of recall quality.  The
review now takes a list ``canaries=[{query, human_id}]`` (config) /
repeated ``--canary QUERY HUMAN_ID`` (CLI), reports each with its rank, and
keeps the single ``canary_query``/``canary_human_id`` fields working.
"""
from __future__ import annotations

from pathlib import Path

from memorymaster.operations import operational_review as review
from memorymaster.operations import review_supervisor

RANKINGS = {
    "why relay": ["mm-x", "mm-relay"],
    "who owns backups": ["mm-backup", "mm-y", "mm-z"],
    "unrelated": ["mm-q"],
}


def _retrieve(_db: Path, query: str) -> list[str]:
    return RANKINGS[query]


def test_every_canary_is_reported_with_its_rank(tmp_path):
    config = review.ReviewConfig(
        db=tmp_path / "memory.db",
        canaries=(("why relay", "mm-relay"), ("who owns backups", "mm-backup")),
    )

    result = review.check_retrieval(config, retrieve=_retrieve)

    assert result.verdict is review.Verdict.PASS
    assert result.counts == {"canaries": 2, "found": 2, "rank:mm-relay": 2, "rank:mm-backup": 1}
    assert "target=mm-relay rank=2" in result.detail
    assert "target=mm-backup rank=1" in result.detail


def test_one_missing_canary_fails_the_check(tmp_path):
    config = review.ReviewConfig(
        db=tmp_path / "memory.db",
        canaries=(("why relay", "mm-relay"), ("unrelated", "mm-backup")),
    )

    result = review.check_retrieval(config, retrieve=_retrieve)

    assert result.verdict is review.Verdict.FAIL
    assert result.counts["found"] == 1
    assert result.counts["rank:mm-backup"] == 0
    assert "target=mm-backup rank=missing" in result.detail


def test_single_canary_fields_keep_their_output_and_combine_with_the_list(tmp_path):
    single = review.ReviewConfig(db=tmp_path / "m.db", canary_query="why relay",
                                 canary_human_id="mm-relay")
    legacy = review.check_retrieval(single, retrieve=_retrieve)
    assert legacy.detail == "target=mm-relay rank=2"
    assert legacy.human_ids == ("mm-x", "mm-relay")

    both = review.ReviewConfig(
        db=tmp_path / "m.db", canary_query="why relay", canary_human_id="mm-relay",
        canaries=(("who owns backups", "mm-backup"), ("why relay", "mm-relay")),
    )
    combined = review.check_retrieval(both, retrieve=_retrieve)
    assert combined.counts["canaries"] == 2, "duplicate canaries are probed once"


def test_cli_accepts_repeated_canaries(tmp_path, monkeypatch):
    seen = {}

    def fake_run_review(config):
        seen["config"] = config
        return []

    monkeypatch.setattr(review, "run_review", fake_run_review)
    code = review.main([
        "--db", str(tmp_path / "memory.db"),
        "--canary", "why relay", "mm-relay",
        "--canary", "who owns backups", "mm-backup",
    ])

    assert code == 0
    assert seen["config"].canaries == (("why relay", "mm-relay"), ("who owns backups", "mm-backup"))


def test_supervisor_forwards_the_canary_list():
    command = review_supervisor._command({
        "python": "python", "db": "memory.db",
        "canary_query": "why relay", "canary_human_id": "mm-relay",
        "canaries": [{"query": "who owns backups", "human_id": "mm-backup"}],
    })

    assert command[command.index("--canary-query") + 1] == "why relay"
    index = command.index("--canary")
    assert command[index:index + 3] == ["--canary", "who owns backups", "mm-backup"]
