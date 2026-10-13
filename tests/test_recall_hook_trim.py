"""T-1351: the prompt-recall hook injects less, never repeats itself, and can be switched off per lane.

Measured 2026-10-06..13: 3297 claims injected into 698 prompts (~5 per prompt, ~5300 characters), 14
cited (0.42%), the same never-cited claims re-injected up to ~50 times a week. Operator decision
2026-10-13: top 2 per prompt, no re-injection within a session, a per-lane switch for a 7-day A/B.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import context_hook, delivery, dense_recall, hook_policy, remote
from memorymaster.recall.qdrant_outbox import ENV_OUTBOX_DIR

TEXTS = (
    "weapon recoil spread pattern decided for the rifle",
    "weapon recoil recovery curve tuned for the pistol",
    "weapon recoil camera shake removed for the sniper",
    "weapon recoil attachments reduce the vertical kick",
)


@pytest.fixture()
def world(tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setenv(ENV_OUTBOX_DIR, str(tmp_path / "outbox"))
    monkeypatch.setenv(dense_recall.LOG_ENV, str(tmp_path / "recall-dense.jsonl"))
    for name in ("QDRANT_URL", "MEMORYMASTER_SCOPE_DEFAULT"):
        monkeypatch.delenv(name, raising=False)
    svc = MemoryService(tmp_path / "trim.db", workspace_root=tmp_path)
    svc.init_db()
    ids = []
    for text in TEXTS:
        claim = svc.store.create_claim(text=text, citations=[CitationInput(source="test://trim")], scope="project:pubgclone")
        lifecycle.transition_claim(svc.store, claim.id, "confirmed", reason="fixture", event_type="validator")
        ids.append(claim.id)
    cwd = tmp_path / "pubgclone"
    cwd.mkdir()
    return {"db": str(svc.store.db_path), "ids": ids, "cwd": str(cwd)}


def _hook(world: dict, session: str) -> dict:
    return {"cwd": world["cwd"], "session_id": session}


def _dense(monkeypatch, scored: list[tuple[int, float]]) -> None:
    monkeypatch.setenv(dense_recall.ENABLE_ENV, "1")
    answer = dense_recall.DenseAnswer(tuple(scored), 30.0, 1.0, len(scored), "m", ())
    monkeypatch.setattr(dense_recall, "search", lambda query, scopes, **_: answer)


def _recall(world: dict, session: str, query: str = "weapon recoil spread") -> list[int]:
    return context_hook.recall(query, db_path=world["db"], return_ids=True, hook_data=_hook(world, session))[1]


# --- AC1: at most 2 claims per prompt, dense and lexical ---------------------------------


def test_dense_injects_at_most_two_claims(world, monkeypatch) -> None:
    _dense(monkeypatch, [(cid, 0.95 - 0.01 * n) for n, cid in enumerate(world["ids"])])
    assert _recall(world, "a") == world["ids"][:2]


def test_lexical_injects_at_most_two_claims(world) -> None:
    assert len(_recall(world, "a")) == 2


def test_the_cap_is_configurable(world, monkeypatch) -> None:
    monkeypatch.setenv(hook_policy.MAX_CLAIMS_ENV, "3")
    assert len(_recall(world, "a")) == 3


def test_recall_without_hook_data_is_not_capped(world) -> None:
    token = context_hook._REQUEST_SCOPE.set("project:pubgclone")  # the scope the hook would derive from its cwd
    try:
        _text, ids = context_hook.recall("weapon recoil spread", db_path=world["db"], return_ids=True)
    finally:
        context_hook._REQUEST_SCOPE.reset(token)
    assert len(ids) == 4  # MCP/CLI callers keep the full block


def test_dense_thresholds_keep_the_operators_values_and_are_configurable(world, monkeypatch) -> None:
    # Operator 2026-10-13 (team-lead ruling): a higher threshold bought no precision; outside stays >= 0.76.
    assert dense_recall.min_score() == 0.68 and dense_recall.outside_min_score() == 0.76
    _dense(monkeypatch, [(world["ids"][0], 0.69)])
    assert _recall(world, "a") == [world["ids"][0]]
    monkeypatch.setenv(dense_recall.MIN_SCORE_ENV, "0.72")
    assert _recall(world, "b") == []


def test_the_lexical_floor_drops_weak_rows(world, monkeypatch) -> None:
    monkeypatch.setenv(hook_policy.LEXICAL_MIN_ENV, "1000000")
    assert _recall(world, "a") == []


# --- AC2: a claim already injected in a session is not injected again --------------------


def test_a_claim_is_not_reinjected_in_the_same_session(world, monkeypatch) -> None:
    _dense(monkeypatch, [(cid, 0.95 - 0.01 * n) for n, cid in enumerate(world["ids"])])
    first = _recall(world, "s1")
    second = _recall(world, "s1")
    third = _recall(world, "s1")
    assert first == world["ids"][:2]
    assert second == world["ids"][2:4]  # the next best fills the slots; the first two are not repeated
    assert third == []  # everything on offer was already injected
    assert _recall(world, "s2") == world["ids"][:2]  # another session is independent


def test_lexical_recall_is_deduplicated_per_session_too(world) -> None:
    first, second = _recall(world, "s1"), _recall(world, "s1")
    assert first and second and not set(first) & set(second)


def test_suppressed_claims_do_not_count_as_accesses(world, monkeypatch) -> None:
    _dense(monkeypatch, [(world["ids"][0], 0.9)])
    seen: list[int] = []
    monkeypatch.setattr(MemoryService, "_record_accesses",
                        lambda self, rows, **k: seen.extend(r["claim"].id for r in rows))
    _recall(world, "s1")
    _recall(world, "s1")
    assert seen == [world["ids"][0]]


def test_without_a_session_id_nothing_is_remembered(world, monkeypatch) -> None:
    _dense(monkeypatch, [(world["ids"][0], 0.9)])
    hook = {"cwd": world["cwd"]}
    one = context_hook.recall("weapon recoil", db_path=world["db"], return_ids=True, hook_data=hook)[1]
    two = context_hook.recall("weapon recoil", db_path=world["db"], return_ids=True, hook_data=hook)[1]
    assert one == two == [world["ids"][0]]


def test_dedup_can_be_turned_off(world, monkeypatch) -> None:
    monkeypatch.setenv(hook_policy.SESSION_DEDUP_ENV, "0")
    _dense(monkeypatch, [(world["ids"][0], 0.9)])
    assert _recall(world, "s1") == _recall(world, "s1") == [world["ids"][0]]


def test_session_start_lets_a_session_see_its_claims_again(world, monkeypatch) -> None:
    _dense(monkeypatch, [(world["ids"][0], 0.9)])
    assert _recall(world, "s1") == [world["ids"][0]]
    assert _recall(world, "s1") == []
    delivery.reset_session("s1")  # SessionStart, including compact/resume
    assert _recall(world, "s1") == [world["ids"][0]]


def test_the_seen_store_expires_and_stays_bounded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(hook_policy.SEEN_DIR_ENV, str(tmp_path / "seen"))
    now = 1_000_000.0
    hook_policy.remember("s", list(range(1, 11)), now=now)
    assert hook_policy.seen_ids("s", now=now + 60) == frozenset(range(1, 11))
    assert hook_policy.seen_ids("s", now=now + hook_policy.SEEN_TTL_SECONDS + 1) == frozenset()
    hook_policy.remember("big", list(range(hook_policy.SEEN_MAX_IDS + 50)), now=now)
    kept = hook_policy.seen_ids("big", now=now)
    assert len(kept) == hook_policy.SEEN_MAX_IDS and max(kept) == hook_policy.SEEN_MAX_IDS + 49  # newest kept
    for n in range(hook_policy.SEEN_MAX_FILES + 100):
        hook_policy.remember(f"session-{n}", [n], now=now + n)
    assert len(list((tmp_path / "seen").glob("*.json"))) <= hook_policy.SEEN_MAX_FILES


def test_a_corrupt_seen_file_means_nothing_was_seen(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(hook_policy.SEEN_DIR_ENV, str(tmp_path))
    hook_policy.remember("s", [1])
    for path in tmp_path.glob("*.json"):
        path.write_text("{not json", encoding="utf-8")
    assert hook_policy.seen_ids("s") == frozenset()
    hook_policy.remember("s", [2])
    assert hook_policy.seen_ids("s") == frozenset({2})


def test_the_seen_store_never_holds_text(world, monkeypatch, tmp_path) -> None:
    _dense(monkeypatch, [(world["ids"][0], 0.9)])
    _recall(world, "s1", query="weapon recoil secretphrase")
    files = list(Path(os.environ[hook_policy.SEEN_DIR_ENV]).glob("*.json"))
    assert files and all(set(json.loads(f.read_text())) == {"ts", "ids"} for f in files)
    assert "secretphrase" not in "".join(f.read_text() for f in files)


# --- AC3: a per-lane/per-cwd switch, in config, default on --------------------------------


def _config(tmp_path, monkeypatch, body: object) -> None:
    path = tmp_path / "recall-hook.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setenv(hook_policy.CONFIG_ENV, str(path))


def test_the_hook_is_on_by_default(tmp_path) -> None:
    assert not hook_policy.disabled_for("G:/Py Apps/whatsappbot-final")
    assert not hook_policy.disabled_for(None)


def test_a_lane_can_be_turned_off_by_repo_name(tmp_path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, {"disabled_cwds": ["whatsappbot-final", "bot-rf-optimizer"]})
    assert hook_policy.disabled_for("G:\\Py Apps\\whatsappbot-final")
    assert hook_policy.disabled_for("G:/Py Apps/whatsappbot-final/.claude/worktrees/t1")  # worktrees too
    assert hook_policy.disabled_for("g:/py apps/BOT-RF-OPTIMIZER")
    assert not hook_policy.disabled_for("G:/Py Apps/whatsappbot")  # a different repo
    assert not hook_policy.disabled_for("G:/Py Apps/memorymaster")


def test_a_path_entry_matches_that_tree_only(tmp_path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, {"disabled_cwds": ["G:/Py Apps/wisp"]})
    assert hook_policy.disabled_for("G:\\Py Apps\\wisp\\sub")
    assert not hook_policy.disabled_for("G:/Py Apps/wispy")
    assert not hook_policy.disabled_for("G:/other/Py Apps/wisp")


def test_the_whole_hook_can_be_turned_off(tmp_path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, {"enabled": False})
    assert hook_policy.disabled_for("G:/anything")


def test_a_broken_config_leaves_the_hook_on(tmp_path, monkeypatch) -> None:
    path = tmp_path / "recall-hook.json"
    path.write_text("[1, 2", encoding="utf-8")
    monkeypatch.setenv(hook_policy.CONFIG_ENV, str(path))
    assert not hook_policy.disabled_for("G:/anything")
    _config(tmp_path, monkeypatch, {"disabled_cwds": "not-a-list"})
    assert not hook_policy.disabled_for("G:/anything")


def test_a_disabled_lane_gets_no_recall_and_no_server_call(tmp_path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, {"disabled_cwds": ["whatsappbot-final"]})
    calls: list[str] = []
    monkeypatch.setattr(remote, "remote_recall", lambda *a, **k: calls.append("remote"))
    assert remote.hook_recall("why x y", {"cwd": "G:/Py Apps/whatsappbot-final"}, db_path="x.db") == ("", "off")
    assert calls == []


def test_other_lanes_still_recall(tmp_path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, {"disabled_cwds": ["whatsappbot-final"]})
    monkeypatch.setattr(remote, "remote_recall", lambda *a, **k: remote.RemoteRecall("# ctx", True))
    assert remote.hook_recall("why x y", {"cwd": "G:/Py Apps/memorymaster"}, db_path="x.db") == ("# ctx", "shared")


def test_the_config_is_read_per_prompt_without_a_restart(tmp_path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, {"disabled_cwds": ["repo"]})
    assert hook_policy.disabled_for("G:/repo")
    _config(tmp_path, monkeypatch, {"disabled_cwds": []})
    assert not hook_policy.disabled_for("G:/repo")


def test_the_env_switch_turns_the_hook_off(monkeypatch) -> None:
    monkeypatch.setenv(hook_policy.ENABLED_ENV, "0")
    assert hook_policy.disabled_for("G:/repo")
