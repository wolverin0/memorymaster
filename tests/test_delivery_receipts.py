"""T-2275: a claim is delivered (session-seen, access counted) only after the adapter's FINAL emission succeeds.

Per-turn receipts (selected -> packed -> emitted, one hook.log line of ids and omission reasons) say why each
claim that did not reach the agent was left out.  Everything is hermetic: the hooks are the shipped templates
rendered into tmp dirs (fixtures of ``test_delivery_audit``), an ephemeral SQLite, a tmp spool and hook log.
"""
from __future__ import annotations

import io
import json
import runpy
import sys

import pytest
from test_delivery_audit import hooked, fallback_world, rendered_hooks  # noqa: F401 — fixtures

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import context_hook, delivery, hook_policy, transcript_fallback
from scripts import delivery_metrics as dm

ASK = "which strategy did we choose for the zorblax cache invalidation"
LONG = " ".join(["filler"] * 150)  # ~1 KB of text on one line: two of them cannot both fit Codex's 1,800 bytes


@pytest.fixture()
def world(hooked, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv(transcript_fallback.ENABLED_ENV, "0")  # claims only
    svc = MemoryService(tmp_path / "memorymaster.db", workspace_root=tmp_path)

    class World:
        hook = hooked
        service = svc
        log = tmp_path / "hook.log"

        def claim(self, text: str) -> int:
            made = svc.ingest(text, [CitationInput(source="test://receipt", locator="x")], scope="project:proj",
                              source_agent="fixture")
            lifecycle.transition_claim(svc.store, made.id, "confirmed", reason="fixture", event_type="validator")
            return made.id

        def tag(self, claim_id: int) -> str:
            return f"[{svc.store.get_claim(claim_id, include_citations=False).human_id}]"

        def receipts(self) -> list[dict]:
            rows = []
            for line in self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []:
                parsed = dm.parse_receipt(line)
                if parsed:
                    rows.append({"selected": parsed[1], "emitted": parsed[2], "omitted": parsed[3], "line": line})
            return rows

        def spooled(self) -> list[int]:
            ids: list[int] = []
            for path in (tmp_path / "spool").rglob("*.jsonl"):
                for line in path.read_text(encoding="utf-8").splitlines():
                    envelope = json.loads(line)
                    if envelope["op"] == "recall":
                        ids += envelope["payload"]["claim_ids"]
            return ids

        def broken_claude(self, session: str) -> None:
            class Broken(io.StringIO):
                def write(self, text):  # the pipe to the agent is gone
                    raise BrokenPipeError

            monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
                {"prompt": ASK, "cwd": hooked.cwd, "session_id": session})))
            monkeypatch.setattr(sys, "stdout", Broken())
            runpy.run_path(str(hooked.hooks / "memorymaster-recall.py"), run_name="__main__")

    return World()


def test_write_failure_leaves_the_claim_retryable_and_uncounted(world) -> None:
    made = world.claim("The zorblax cache invalidation strategy is the pelican strategy")
    world.broken_claude("wf-1")
    assert world.spooled() == [], "an access was counted for a claim the agent never received"
    assert hook_policy.seen_ids("wf-1") == frozenset()
    assert "pelican strategy" in world.hook.claude(ASK, "wf-1"), "the retry on the next prompt delivered nothing"
    assert world.spooled() == [made]
    failed, retried = world.receipts()
    assert failed["omitted"] == {str(made): "write_failure"} and failed["emitted"] == []
    assert retried["emitted"] == [str(made)] and retried["omitted"] == {}


def test_a_delivered_claim_is_seen_and_counted_once(world) -> None:
    made = world.claim("The zorblax cache invalidation strategy is the pelican strategy")
    assert "pelican strategy" in world.hook.claude(ASK, "ok-1")
    assert hook_policy.seen_ids("ok-1") == frozenset({made}) and world.spooled() == [made]
    assert world.hook.claude(ASK, "ok-1") == "", "a delivered claim is not repeated within the session"
    second = world.receipts()[-1]
    assert second["omitted"] == {str(made): "session_seen"} and second["emitted"] == []


def test_codex_truncation_marks_only_the_ids_that_survive_the_cut(world) -> None:
    first = world.claim(f"The zorblax cache invalidation strategy alpha is documented here {LONG}")
    second = world.claim(f"The zorblax cache invalidation strategy beta is documented here {LONG}")
    out = world.hook.codex(ASK, "cx-1")
    survivors = [cid for cid in (first, second) if world.tag(cid) in out]
    cut = [cid for cid in (first, second) if cid not in survivors]
    assert len(survivors) == 1 and "[... truncated]" in out, "precondition: the 1,800-byte cut dropped one claim"
    assert hook_policy.seen_ids("cx-1") == frozenset(survivors) and world.spooled() == survivors
    receipt = world.receipts()[-1]
    assert receipt["omitted"] == {str(cut[0]): "adapter_truncation"}
    assert world.tag(cut[0]) in world.hook.codex(ASK, "cx-1"), "the cut claim is delivered on the next prompt"


def test_receipts_explain_every_omitted_id(world) -> None:
    ids = [world.claim(f"The zorblax cache invalidation strategy number {n} is documented here {LONG}")
           for n in range(4)]
    world.hook.claude(ASK, "r-slot")  # 4 selected, 2 packed: the others hit the slot cap
    world.hook.claude(ASK, "r-slot")  # the packed ones are now seen
    world.hook.codex(ASK, "r-cut")  # 2 packed, the Codex cut drops one
    world.broken_claude("r-fail")  # nothing emitted
    receipts = world.receipts()
    assert len(receipts) == 4
    reasons = {reason for receipt in receipts for reason in receipt["omitted"].values()}
    assert {"slot_cap", "session_seen", "adapter_truncation", "write_failure"} <= reasons
    for receipt in receipts:
        missing = [cid for cid in receipt["selected"] if cid not in receipt["emitted"]]
        assert missing and all(cid in receipt["omitted"] for cid in missing), receipt
        assert all(reason in delivery.OMISSION_REASONS for reason in receipt["omitted"].values())
        assert set(receipt["selected"]) <= {str(i) for i in ids}
    report = dm.receipt_explanation(world.log, dm.parse_ts("2000-01-01T00:00:00+00:00"))
    assert report["turns_with_omissions"] == 4 and report["share_omissions_explained"] == 1.0


def test_byte_budget_and_slot_cap_are_named_for_the_rows_left_out(world) -> None:
    ids = [world.claim(f"The zorblax cache invalidation strategy {n} is documented here {LONG}") for n in range(3)]
    rows = [{"claim": world.service.store.get_claim(cid, include_citations=False)} for cid in ids]
    _, rendered = context_hook._render_recall_lines(rows, budget=10)
    assert rendered == []
    assert context_hook._omission_reasons(rows, rendered, None) == dict.fromkeys(ids, "byte_budget")
    _, rendered = context_hook._render_recall_lines(rows, budget=2000, limit=1)
    assert len(rendered) == 1
    assert context_hook._omission_reasons(rows, rendered, 1) == dict.fromkeys(ids[1:], "slot_cap")


def test_receipts_and_the_staged_turn_carry_no_claim_or_prompt_text(world, tmp_path) -> None:
    world.claim("The zorblax cache invalidation strategy is the pelican strategy")
    world.claim("The zorblax cache invalidation window is quartzite seventeen minutes")
    world.hook.claude(ASK, "tx-1")
    world.broken_claude("tx-2")
    lines = [receipt["line"] for receipt in world.receipts()]
    assert len(lines) == 2
    for word in ("zorblax", "pelican", "quartzite", "strategy", "invalidation", "choose"):
        assert not any(word in line for line in lines), f"{word!r} leaked into a receipt"
    # the turn parked between recall and emission holds ids and tags only
    data = {"cwd": world.hook.cwd, "session_id": "tx-3", hook_policy.DEFER_KEY: "tx3"}
    context_hook.recall(ASK, db_path=str(world.service.store.db_path), hook_data=data)
    staged = (tmp_path / "seen").glob("*.turn.json")
    raw = "".join(path.read_text(encoding="utf-8") for path in staged)
    assert raw and not any(word in raw for word in ("zorblax", "pelican", "quartzite", "choose"))


def test_recall_defers_seen_and_access_only_when_the_adapter_asks(world) -> None:
    made = world.claim("The zorblax cache invalidation strategy is the pelican strategy")
    db = str(world.service.store.db_path)
    context_hook.recall(ASK, db_path=db, hook_data={"cwd": world.hook.cwd, "session_id": "d-legacy"})
    assert hook_policy.seen_ids("d-legacy") == frozenset({made}), "a caller without the flag keeps the old contract"
    assert world.spooled() == [made]
    context_hook.recall(ASK, db_path=db, hook_data={"cwd": world.hook.cwd, "session_id": "d-defer",
                                                    hook_policy.DEFER_KEY: "d1"})
    assert hook_policy.seen_ids("d-defer") == frozenset() and world.spooled() == [made], "nothing before emission"
    receipt = delivery.confirm_turn({"session_id": "d-defer", hook_policy.DEFER_KEY: "d1"}, "test",
                                    emitted=f"- {world.tag(made)} x", query=ASK)
    assert receipt["emitted"] == [made] and hook_policy.seen_ids("d-defer") == frozenset({made})
    assert world.spooled() == [made, made]


@pytest.fixture()
def staged(tmp_path, monkeypatch):
    """A stand-in for the ranker that stages a turn the way recall does, for the agy and OpenCode adapters."""
    from memorymaster.core import hook_log
    from memorymaster.recall import remote

    monkeypatch.setenv("MEMORYMASTER_RECALL_SEEN_DIR", str(tmp_path / "seen"))
    monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setattr(hook_log, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(hook_log, "_LOG_FILE", tmp_path / "hook.log")
    monkeypatch.delenv("MEMORYMASTER_AGY_HOOK_INTERNAL", raising=False)
    monkeypatch.delenv("AGY_BATCH_PROFILE", raising=False)

    def ranker(query, data, *, db_path, **_):
        assert hook_policy.defers_delivery(data)
        hook_policy.stage_turn(data["session_id"], data[hook_policy.DEFER_KEY], {
            "turn": "t1", "db_path": str(tmp_path / "m.db"), "ranked_n": 2, "selected": [7, 8],
            "packed": [[7, "mm-0007"]], "omitted": {"8": "slot_cap"}})
        return "\n- [mm-0007] a fact", "shared"

    monkeypatch.setattr(remote, "hook_recall", ranker)
    return tmp_path


def _spooled(root) -> list[int]:
    return [i for p in (root / "spool").rglob("*.jsonl") for line in p.read_text().splitlines()
            for i in json.loads(line)["payload"]["claim_ids"]]


def test_opencode_confirms_after_its_write_and_not_when_the_write_fails(staged, monkeypatch) -> None:
    from memorymaster.recall import remote
    from memorymaster.surfaces import hook_recall

    monkeypatch.setattr(hook_recall, "hook_recall", remote.hook_recall)  # the adapter bound the real one at import

    def run(session: str, stdout) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
            {"prompt": "why was the strategy chosen", "cwd": str(staged), "session_id": session})))
        monkeypatch.setattr(sys, "stdout", stdout)
        assert hook_recall.main(["--cli", "opencode"]) == 0

    class Broken(io.StringIO):
        def write(self, text):
            raise BrokenPipeError

    run("ses-a", Broken())
    assert hook_policy.seen_ids("opencode:ses-a") == frozenset() and _spooled(staged) == []
    run("ses-a", io.StringIO())
    assert hook_policy.seen_ids("opencode:ses-a") == frozenset({7}) and _spooled(staged) == [7]


def test_agy_confirms_after_its_write_and_not_when_the_write_fails(staged, monkeypatch) -> None:
    from memorymaster.surfaces import agy_hooks

    payload = {"conversationId": "agy-1", "workspacePaths": [str(staged)], "lastUserInput": "why this strategy",
               "modelName": "m", "transcriptPath": ""}

    def run(stdout) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys, "stdout", stdout)
        assert agy_hooks.main("PreInvocation", db_path="unused.db") == 0

    class Broken(io.StringIO):
        def write(self, text):
            raise BrokenPipeError

    run(Broken())
    assert hook_policy.seen_ids("agy-1") == frozenset() and _spooled(staged) == []
    out = io.StringIO()
    run(out)
    assert "a fact" in out.getvalue()
    assert hook_policy.seen_ids("agy-1") == frozenset({7}) and _spooled(staged) == [7]


def test_an_unconfirmed_turn_expires_and_leaves_its_claims_retryable(world, monkeypatch) -> None:
    world.claim("The zorblax cache invalidation strategy is the pelican strategy")
    db = str(world.service.store.db_path)
    context_hook.recall(ASK, db_path=db, hook_data={"cwd": world.hook.cwd, "session_id": "e-1",
                                                    hook_policy.DEFER_KEY: "e1"})
    assert hook_policy.take_turn("e-1", "e1", now=10**12) is None  # long expired: nothing to confirm
    assert delivery.confirm_turn({"session_id": "e-1", hook_policy.DEFER_KEY: "e1"}, "test", emitted="x") is None
    assert hook_policy.seen_ids("e-1") == frozenset()


def test_a_hook_and_a_server_on_different_state_dirs_do_not_loop(world, tmp_path, monkeypatch) -> None:
    """The staged turn never reaches the hook (another HOME): what was emitted is still marked seen."""
    from memorymaster.recall import remote

    made = world.claim("The zorblax cache invalidation strategy is the pelican strategy")
    real = remote.hook_recall

    def server_side(query, data, **kwargs):  # recall, staging and the seen read happen in the "server" state dir
        with monkeypatch.context() as patch:
            patch.setenv("MEMORYMASTER_RECALL_SEEN_DIR", str(tmp_path / "server-seen"))
            return real(query, data, **kwargs)

    monkeypatch.setattr(remote, "hook_recall", server_side)
    assert "pelican strategy" in world.hook.claude(ASK, "split-1")
    assert hook_policy.seen_ids("split-1") == frozenset({made}), "the hook marks what it emitted"
    assert world.hook.claude(ASK, "split-1") == "", "the claim is injected again: a re-injection loop"
    missing = [line for line in world.log.read_text(encoding="utf-8").splitlines() if "turn=missing" in line]
    assert len(missing) == 1 and "reason=no_staged_turn" in missing[0] and f"emitted={made}" in missing[0]
    assert world.spooled() == [made]
    assert not any(word in missing[0] for word in ("zorblax", "pelican", "strategy"))
    report = dm.receipt_explanation(world.log, dm.parse_ts("2000-01-01T00:00:00+00:00"))
    assert report["turns_without_staged_turn"] == 1


def test_a_late_stage_never_confirms_another_turns_claims(world) -> None:
    db = str(world.service.store.db_path)
    first, second, third = (world.claim(f"The zorblax cache invalidation strategy {n} is documented") for n in range(3))
    session = "late-1"

    def stage(turn: str, claim_id: int, tag: str | None) -> None:
        hook_policy.stage_turn(session, turn, {"turn": turn, "db_path": db, "ranked_n": 1, "selected": [claim_id],
                                               "packed": [[claim_id, tag]], "omitted": {}})

    def data(turn: str) -> dict:
        return {"session_id": session, hook_policy.DEFER_KEY: turn}

    # prompt 1: its hook confirms before recall's stage arrived
    receipt = delivery.confirm_turn(data("t1"), "claude", emitted=f"- {world.tag(first)} x", query=ASK, db_path=db)
    assert receipt["turn"] == "missing" and hook_policy.seen_ids(session) == frozenset({first})
    # the stage of prompt 1 lands late; prompt 2 is staged and confirmed meanwhile
    stage("t1", first, world.tag(first)[1:-1])
    stage("t2", second, world.tag(second)[1:-1])
    receipt = delivery.confirm_turn(data("t2"), "claude", emitted=f"- {world.tag(second)} x", query=ASK, db_path=db)
    assert receipt["turn"] == "t2" and receipt["emitted"] == [second]
    assert hook_policy.seen_ids(session) == frozenset({first, second})
    assert hook_policy.take_turn(session, "t1") is not None, "prompt 2 consumed the late stage of prompt 1"
    # a claim without a human id counts as emitted only for the turn that staged it
    stage("t3", third, None)
    assert delivery.confirm_turn(data("t4"), "claude", emitted="some text", query=ASK, db_path=db) is None
    assert hook_policy.seen_ids(session) == frozenset({first, second})
    assert delivery.confirm_turn(data("t3"), "claude", emitted="some text", query=ASK)["emitted"] == [third]


def test_codex_write_failure_does_not_suppress_an_identical_retry(world, monkeypatch) -> None:
    made = world.claim("The zorblax cache invalidation strategy is the pelican strategy")

    class Broken(io.StringIO):
        def write(self, text):
            raise BrokenPipeError

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": ASK, "cwd": world.hook.cwd,
                                                              "session_id": "cxf-1"})))
    adapter = runpy.run_path(str(world.hook.hooks / "memorymaster-codex-recall.py"))
    monkeypatch.setattr(sys, "stdout", Broken())
    assert adapter["main"]("UserPromptSubmit") == 0
    assert hook_policy.seen_ids("cxf-1") == frozenset() and world.spooled() == []
    assert world.tag(made) in world.hook.codex(ASK, "cxf-1"), "the identical retry was suppressed for 300 s"
    assert hook_policy.seen_ids("cxf-1") == frozenset({made})


def test_the_seen_ids_a_client_sends_are_bounded_and_real_ints(world) -> None:
    sent = [*range(300), True, "7", 3.0, None]  # a bool is an int in Python and is no claim id
    seen = context_hook._session_seen({"session_id": "cap-1", "seen_ids": sent})
    assert seen == frozenset(range(104, 300)) and len(seen) <= hook_policy.SEEN_MAX_IDS
    assert context_hook._session_seen({"session_id": "cap-1", "seen_ids": "nope"}) == frozenset()


def test_a_tilde_suffixed_tag_still_resolves_to_its_claim(world) -> None:
    db = str(world.service.store.db_path)
    made = world.claim("The zorblax cache invalidation strategy is the pelican strategy")
    human_id = world.tag(made)[1:-1]
    receipt = delivery.confirm_turn({"session_id": "tilde-1", hook_policy.DEFER_KEY: "x1"}, "claude",
                                    emitted=f"- [{human_id}~2] a claim", query=ASK, db_path=db)
    assert receipt["turn"] == "missing" and receipt["emitted"] == [made]
    assert hook_policy.seen_ids("tilde-1") == frozenset({made})
