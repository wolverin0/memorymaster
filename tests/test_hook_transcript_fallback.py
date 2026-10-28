"""T-1862 (review F2): the prompt hook appends raw past conversation when claims do not fill its cap.

Synthetic transcripts only; the sidecar index and the claims DB live in tmp dirs.  Every test goes through
``context_hook.recall(..., hook_data=...)``, the entry of the UserPromptSubmit hook.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import context_hook, transcript_fallback
from memorymaster.recall import transcript_index as ti
from memorymaster.recall.recall_tokenizer import centered_excerpt
from memorymaster.recall.verbatim_store import ensure_verbatim_schema, store_verbatim

ASK = "which firmware version did we choose for the router"


def _rec(role: str, text: str, ts: str, cwd: str) -> str:
    return json.dumps({"type": role, "timestamp": ts, "cwd": cwd,
                       "message": {"role": role, "content": [{"type": "text", "text": text}]}})


@pytest.fixture()
def world(tmp_path: Path, monkeypatch):
    for name in ("QDRANT_URL", "MEMORYMASTER_SCOPE_DEFAULT", transcript_fallback.ENABLED_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("MEMORYMASTER_RECALL_HOOK_MAX_CLAIMS", "2")
    idx = tmp_path / "idx" / "transcripts.db"
    monkeypatch.setenv(ti.INDEX_ENV, str(idx))
    svc = MemoryService(tmp_path / "claims.db", workspace_root=tmp_path)
    svc.init_db()
    here, elsewhere = tmp_path / "here", tmp_path / "elsewhere"
    here.mkdir()
    elsewhere.mkdir()

    class World:
        db = str(svc.store.db_path)
        index = idx
        cwd = str(here)
        other_cwd = str(elsewhere)

        def transcripts(self, *records: tuple[str, str, str, str]) -> None:
            root = tmp_path / "projects"
            (root / "p").mkdir(parents=True, exist_ok=True)
            for n, cwd in enumerate(dict.fromkeys(r[3] for r in records)):  # one file per project: a file has one scope
                lines = [_rec(role, text, ts, c) for role, text, ts, c in records if c == cwd]
                (root / "p" / f"s{n}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            ti.index_transcripts(root, idx)

        def claim(self, text: str) -> int:
            made = svc.ingest(text, [CitationInput(source="test://fb", locator="x")], scope="project:here",
                              source_agent="fixture")
            lifecycle.transition_claim(svc.store, made.id, "confirmed", reason="fixture", event_type="validator")
            return made.id

        def ask(self, question: str = ASK, **hook) -> tuple[str, list[int]]:
            return context_hook.recall(question, db_path=self.db, return_ids=True,
                                       hook_data={"cwd": self.cwd, **hook})

    return World()


def _long(prefix: str, needle: str) -> str:
    return prefix + " ".join(f"filler{i}" for i in range(140)) + " " + needle + " " + "tail " * 40


def test_fragments_fill_the_slots_claims_leave_free(world) -> None:
    world.transcripts(("assistant", _long("Notes. ", "The router firmware version chosen was 7.18.2 after the rollback."),
                       "2026-09-14T10:00:00Z", world.cwd))
    text, ids = world.ask()
    assert ids == []
    assert text.startswith("# Raw past conversation")
    assert "possibly outdated" in text and "not curated memory" in text
    assert "[2026-09-14 project:here]" in text
    assert "7.18.2" in text  # the snippet is centred on the match, 700+ characters into the fragment
    assert "filler0 " not in text  # ... and does not start at the fragment head
    assert text.count("\n- [") <= transcript_fallback.MAX_FRAGMENTS


def test_only_this_projects_fragments_are_injected(world) -> None:
    world.transcripts(
        ("assistant", "The router firmware version chosen was 9.9.9 for the other project", "2026-09-01T00:00:00Z",
         world.other_cwd),
        ("assistant", "The router firmware version we chose was 7.18.2 here", "2026-08-01T00:00:00Z", world.cwd),
    )
    text, _ = world.ask()
    assert "7.18.2" in text and "9.9.9" not in text
    world.cwd, other = world.other_cwd, world.cwd  # the other project asks: it sees its own fragment only
    text, _ = world.ask()
    assert "9.9.9" in text and "7.18.2" not in text
    world.cwd = other


def test_a_scope_without_fragments_gets_nothing(world, tmp_path) -> None:
    world.transcripts(("assistant", "The router firmware version we chose was 7.18.2", "2026-08-01T00:00:00Z",
                       world.other_cwd))
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    world.cwd = str(stranger)
    assert world.ask() == ("", [])


def test_claims_come_first_and_fragments_take_only_the_free_slots(world) -> None:
    claim = world.claim("The router firmware version is pinned until the lab is rebuilt")
    world.transcripts(
        ("assistant", "The router firmware version we chose was 7.18.2 here", "2026-08-01T00:00:00Z", world.cwd),
        ("user", "Remember the router firmware version 7.18.2 decision from the lab session", "2026-08-02T00:00:00Z",
         world.cwd),
    )
    text, ids = world.ask()
    assert ids == [claim]
    assert text.startswith("# Memory Context") and "\n\n# Raw past conversation" in text
    assert text.count("\n- [20") == 1  # cap 2, one claim: one fragment


def test_a_full_cap_gets_no_fragments(world, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_RECALL_HOOK_MAX_CLAIMS", "1")
    world.claim("The router firmware version is pinned until the lab is rebuilt")
    world.transcripts(("assistant", "The router firmware version we chose was 7.18.2 here", "2026-08-01T00:00:00Z",
                       world.cwd))
    text, ids = world.ask()
    assert len(ids) == 1 and "Raw past conversation" not in text


def test_fragments_are_not_claims(world, monkeypatch) -> None:
    from memorymaster.recall import hook_policy

    world.transcripts(("assistant", "The router firmware version we chose was 7.18.2 here", "2026-08-01T00:00:00Z",
                       world.cwd))
    text, ids = world.ask(session_id="sess-1")
    assert "7.18.2" in text and ids == []
    assert hook_policy.seen_ids("sess-1") == frozenset()  # nothing was remembered as injected


# --- the gate and the failure modes -------------------------------------------------------------


def test_default_is_on_only_when_the_index_exists(world, monkeypatch) -> None:
    assert not transcript_fallback.enabled()  # nothing indexed yet
    world.transcripts(("assistant", "The router firmware version we chose was 7.18.2", "2026-08-01T00:00:00Z",
                       world.cwd))
    assert transcript_fallback.enabled()
    monkeypatch.setenv(transcript_fallback.ENABLED_ENV, "0")
    assert not transcript_fallback.enabled()
    assert world.ask() == ("", [])
    monkeypatch.setenv(transcript_fallback.ENABLED_ENV, "1")
    assert transcript_fallback.enabled()


def test_an_unreadable_index_fails_open(world) -> None:
    claim = world.claim("The router firmware version is pinned until the lab is rebuilt")
    world.index.parent.mkdir(parents=True, exist_ok=True)
    world.index.write_bytes(b"this is not a sqlite database" * 40)
    text, ids = world.ask()
    assert ids == [claim] and text.startswith("# Memory Context") and "Raw past conversation" not in text


def test_a_search_error_fails_open(world, monkeypatch) -> None:
    claim = world.claim("The router firmware version is pinned until the lab is rebuilt")
    world.transcripts(("assistant", "The router firmware version we chose was 7.18.2", "2026-08-01T00:00:00Z", world.cwd))

    def boom(*args, **kwargs):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(ti, "search", boom)
    text, ids = world.ask()
    assert ids == [claim] and "Raw past conversation" not in text


def test_secrets_in_a_fragment_are_redacted(world) -> None:
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
    world.transcripts(("user", f"The router firmware version we chose was 7.18.2 and the key was {secret} then",
                       "2026-08-01T00:00:00Z", world.cwd))
    text, _ = world.ask()
    assert "7.18.2" in text and secret not in text and "[REDACTED" in text


def test_an_off_topic_fragment_is_not_injected(world) -> None:
    world.transcripts(("assistant", "Remember to water the plants and the garden version of the schedule",
                       "2026-08-01T00:00:00Z", world.cwd))
    assert world.ask() == ("", [])


def test_the_verbatim_stream_is_bypassed_in_the_hook_while_the_fallback_is_active(world, monkeypatch) -> None:
    from memorymaster.recall import verbatim_recall

    world.transcripts(("assistant", "The router firmware version we chose was 7.18.2", "2026-08-01T00:00:00Z", world.cwd))
    monkeypatch.setenv("MEMORYMASTER_RECALL_VERBATIM", "1")

    def must_not_run(*args, **kwargs):
        raise AssertionError("the hook carries one raw-evidence mechanism")

    monkeypatch.setattr(verbatim_recall, "recall_verbatim", must_not_run)
    assert "7.18.2" in world.ask()[0]


# --- review F2: the verbatim merge no longer drops a hit that shares a scope with some claim ----


@pytest.mark.parametrize("hook", [True, False])
def test_verbatim_hit_survives_a_same_scope_claim(world, monkeypatch, hook) -> None:
    monkeypatch.setenv(transcript_fallback.ENABLED_ENV, "0")  # the older stream is the one under test
    monkeypatch.setenv("MEMORYMASTER_RECALL_VERBATIM", "1")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_VERBATIM", "1.0")
    monkeypatch.setenv("MEMORYMASTER_SCOPE_DEFAULT", "project:here")
    world.claim("Router firmware needs regular maintenance")
    ensure_verbatim_schema(world.db)
    store_verbatim(world.db, "s1", "user", "Router firmware chosen was version 7.18.2 after the lab rollback",
                   scope="project:here")
    extra = {"hook_data": {"cwd": world.cwd}} if hook else {}
    text = context_hook.recall("router firmware version", db_path=world.db, **extra)
    assert "7.18.2" in text


# --- the snippet --------------------------------------------------------------------------------


def test_centered_excerpt_windows_around_the_densest_match() -> None:
    body = "alpha " * 200 + "the replication lag was the redis deploy timeout cause " + "omega " * 200
    out = centered_excerpt(body, "redis deploy timeout replication", 200)
    assert len(out) <= 210 and out.startswith("…") and out.endswith("…")
    for word in ("replication", "redis", "deploy", "timeout"):
        assert word in out


def test_centered_excerpt_keeps_short_text_whole_and_falls_back_to_the_head() -> None:
    assert centered_excerpt("short text", "anything", 200) == "short text"
    assert centered_excerpt("x" * 500, "zebra", 100).startswith("xxxx")
