"""T-1862 (external review F1/F4/F8): the prompt hook retrieves, gates and orders with the WHOLE question.

Each test is a repro from the review, run through ``context_hook.recall(..., hook_data=...)`` -- the
exact entry of the UserPromptSubmit hook -- so what is asserted is the FINAL injected block, not an
intermediate ranking.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import context_hook
from memorymaster.recall.recall_tokenizer import _candidate_tokens

SCOPE = "project:pipeline"


@pytest.fixture()
def world(tmp_path: Path, monkeypatch):
    for name in ("QDRANT_URL", "MEMORYMASTER_SCOPE_DEFAULT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("MEMORYMASTER_RECALL_HOOK_MAX_CLAIMS", "3")
    monkeypatch.setenv("MEMORYMASTER_RECALL_TRANSCRIPT_FALLBACK", "0")  # claims only, here
    svc = MemoryService(tmp_path / "pipeline.db", workspace_root=tmp_path)
    svc.init_db()
    cwd = tmp_path / "pipeline"
    cwd.mkdir()

    class World:
        db = str(svc.store.db_path)
        service = svc
        cwd_path = str(cwd)
        count = 0

        def add(self, text: str, confidence: float | None = None, **kw) -> int:
            self.count += 1
            claim = svc.ingest(
                text, [CitationInput(source=f"test://pipeline-{self.count}", locator="fixture")],
                scope=SCOPE, source_agent=f"fixture-{self.count}", **kw,
            )
            lifecycle.transition_claim(svc.store, claim.id, "confirmed", reason="fixture", event_type="validator")
            if confidence is not None:
                with svc.store.connect() as conn:
                    conn.execute("UPDATE claims SET confidence = ? WHERE id = ?", (confidence, claim.id))
                    conn.commit()
            return claim.id

        def ask(self, question: str) -> list[int]:
            return context_hook.recall(
                question, db_path=self.db, return_ids=True, hook_data={"cwd": self.cwd_path},
            )[1]

    return World()


# --- tokenizer: identifiers are kept (F1) -----------------------------------------------------


def test_inline_code_contents_and_path_identifiers_survive_tokenising() -> None:
    assert "fstrim" in _candidate_tokens("Why did we stop using `fstrim`?")
    assert "context_hook" in _candidate_tokens("what does memorymaster/recall/context_hook.py do")
    assert "secretblock" not in _candidate_tokens("see\n```\nsecretblock = 1\n```\nplease")  # fenced code stays out
    assert "example" not in _candidate_tokens("read https://example.com/page")  # urls stay out


# --- F1: one salient full-question search, then a bounded per-term supplement ------------------


def test_late_discriminating_term_is_not_crowded_out_by_early_generic_ones(world) -> None:
    for i in range(4):
        world.add(f"redis cache note {i}")
        world.add(f"deploy checklist note {i}")
        world.add(f"timeout budget note {i}")
    answer = world.add(
        "Redis deploy timeout is caused by replication lag when a replica resyncs during a rolling restart"
    )
    ids = world.ask("redis deploy timeout replication")
    assert ids and ids[0] == answer


def test_every_selected_term_is_searched_not_only_the_first_six(world, monkeypatch) -> None:
    """T-1877 (review F1): 7 identifiers selected, the answer holds only the 7th; distractors hold 1-6."""
    monkeypatch.setenv("MEMORYMASTER_RECALL_HOOK_MAX_CLAIMS", "10")
    terms = [f"zatoken{n}" for n in range(1, 8)]
    for i in range(5):
        world.add(f"distractor {i} mentions {' '.join(terms[:6])}")
    for i in range(4):  # corpus frequency of the 7th term equals the others', so no term outranks it by rarity
        world.service.ingest(
            f"unrelated project note {i} about {terms[6]}", [CitationInput(source=f"test://other-{i}", locator="f")],
            scope="project:elsewhere", source_agent=f"other-{i}",
        )
    answer = world.add(f"the real answer lives behind {terms[6]} only")
    ids = world.ask("what is " + " ".join(terms))
    assert answer in ids


def test_backticked_identifier_is_searched(world) -> None:
    world.add("The old linter was obsolete so we stopped using it")
    answer = world.add("fstrim was removed from the weekly cron because it stalled the NAS volume")
    ids = world.ask("Why did we stop using `fstrim`?")
    assert answer in ids


# --- F8: whole-token coverage, independent of confidence ---------------------------------------


def test_port_does_not_match_report(world) -> None:
    noise = world.add("Quarterly report on redis usage", confidence=1.0)
    ids = world.ask("redis port")
    assert noise not in ids  # one real term of two ("port" only matched inside "report")


def test_single_generic_term_row_is_gated_even_with_top_confidence(world) -> None:
    for i in range(4):  # a pool where "deploy" is rare: its BM25 weight alone lifts a one-term row over the old floor
        world.add(f"redis cache eviction note number {i}", confidence=0.2)
        world.add(f"timeout budget note number {i}", confidence=0.2)
    weak = world.add("deploy window is Tuesday", confidence=1.0)
    strong = world.add("redis deploy timeout was raised to 30s", confidence=0.5)
    ids = world.ask("redis deploy timeout")
    assert strong in ids and weak not in ids


def test_a_row_with_one_exact_identifier_is_kept(world) -> None:
    hit = world.add("fstrim discard is disabled on the NAS", confidence=0.5)
    assert hit in world.ask("what happened with `fstrim` yesterday")


# --- F4: a claim with a pending/explicit correction never outranks the correction ---------------


def test_superseded_claim_does_not_outrank_its_correction_in_the_hook(world) -> None:
    old = world.add("fstrim runs weekly on the NAS volume via cron", confidence=0.97)
    new = world.add(
        "fstrim runs monthly on the NAS volume via cron after the stall", confidence=0.95, supersedes_claim_id=old,
    )
    ids = world.ask("how often does fstrim run on the NAS volume via cron")
    assert new in ids
    assert old not in ids or ids.index(new) < ids.index(old)


def test_dense_path_also_demotes_the_superseded_claim(world, monkeypatch) -> None:
    from memorymaster.recall import dense_recall

    old = world.add("fstrim runs weekly on the NAS volume via cron", confidence=0.97)
    new = world.add("fstrim runs monthly on the NAS volume", confidence=0.95, supersedes_claim_id=old)
    monkeypatch.setenv(dense_recall.ENABLE_ENV, "1")
    answer = dense_recall.DenseAnswer(((old, 0.95), (new, 0.9)), 30.0, 1.0, 2, "m", ())
    monkeypatch.setattr(dense_recall, "search", lambda query, scopes, **_: answer)
    ids = world.ask("how often does fstrim run")
    assert new in ids and (old not in ids or ids.index(new) < ids.index(old))


def test_a_pending_predecessor_without_its_correction_is_not_injected(world) -> None:
    """T-1877 (review F4): the correction is not retrieved, the disputed predecessor must not be injected alone."""
    old = world.add("fstrim runs weekly on the NAS volume via cron", confidence=0.97)
    world.add("Trim schedule moved to monthly", supersedes_claim_id=old)  # shares no query term: not in the pool
    ids = world.ask("fstrim NAS volume cron")
    assert old not in ids


# --- F8: an exact identifier is not a stemmed word --------------------------------------------


def test_identifier_does_not_match_its_stemmed_variants(world) -> None:
    """T-1877 (review F8): `rate` must not pass the identifier gate through "rated" (both stem to "rat")."""
    noise = world.add("The release was rated poorly", confidence=1.0)
    ids = world.ask("what is the `rate` limit")
    assert noise not in ids


def test_identifier_still_keeps_its_exact_row(world) -> None:
    hit = world.add("rate limiter is capped at 30 requests", confidence=0.5)
    assert hit in world.ask("what is the `rate` limit")
