"""Delivery policy: suppress repetition, never suppress recovery or real questions."""
import json

import pytest

from memorymaster.recall.delivery import deliver, is_automated, recall_block, reset_session, would_deliver


@pytest.mark.parametrize("marker", [
    "[SYSTEM NOTIFICATION - NOT USER INPUT]", "<task-notification>",
    "AUTO-SAVE checkpoint", "FLEET agents=",
])
def test_only_structural_machine_markers_skip(marker):
    assert is_automated("  " + marker + " tick")
    assert not is_automated("Please explain " + marker + " in this log")


def test_delivery_is_scoped_bounded_and_resets(tmp_path):
    out = []
    data = {"session_id": "same-prefix-123456-session-one", "cwd": "project-a"}
    assert deliver(data, "memory", out.append, state_dir=tmp_path, now=10)
    assert not deliver(data, "memory", out.append, state_dir=tmp_path, now=11)
    assert deliver({**data, "cwd": "project-b"}, "memory", out.append, state_dir=tmp_path, now=12)
    assert deliver(data, "changed", out.append, state_dir=tmp_path, now=13)
    assert deliver({**data, "session_id": "same-prefix-123456-session-two"},
                   "changed", out.append, state_dir=tmp_path, now=14)
    reset_session(data["session_id"], state_dir=tmp_path)
    assert deliver(data, "changed", out.append, state_dir=tmp_path, now=15)
    assert deliver(data, "changed", out.append, state_dir=tmp_path, now=316)
    assert all(json.loads(value)["hookSpecificOutput"]["additionalContext"] for value in out)
    assert "changed" not in "".join(p.read_text() for p in tmp_path.iterdir())


def test_failed_output_does_not_advance_delivery(tmp_path):
    data = {"session_id": "delivery-failure"}
    def fail(_):
        raise BrokenPipeError
    with pytest.raises(BrokenPipeError):
        deliver(data, "context", fail, state_dir=tmp_path, now=10)
    assert deliver(data, "context", lambda _: None, state_dir=tmp_path, now=11)


def test_missing_identity_and_unreadable_state_fail_open(tmp_path):
    assert deliver({}, "context", lambda _: None, state_dir=tmp_path)
    assert deliver({}, "context", lambda _: None, state_dir=tmp_path)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("fixture")
    assert deliver({"session_id": "s"}, "context", lambda _: None, state_dir=blocked)
    assert not deliver({"session_id": "s"}, "", lambda _: None, state_dir=tmp_path)


def test_clock_reversal_and_corrupt_state_do_not_hide_context(tmp_path):
    data = {"session_id": "s"}
    assert deliver(data, "context", lambda _: None, state_dir=tmp_path, now=10)
    assert deliver(data, "context", lambda _: None, state_dir=tmp_path, now=9)
    next(tmp_path.iterdir()).write_text("not-json")
    assert deliver(data, "context", lambda _: None, state_dir=tmp_path, now=10)


def test_deliver_lone_surrogate_prompt_and_session(tmp_path):
    # The 2026-10-20/22 hook error: JSON can decode a lone UTF-16 surrogate.
    data = json.loads(r'{"prompt":"explain \udc8f recall","session_id":"s\ud800","cwd":"repo\udc8f"}')
    context = recall_block(data["prompt"])
    out = []
    assert deliver(data, context, out.append, state_dir=tmp_path, now=10)
    assert json.loads(out[0])["hookSpecificOutput"]["additionalContext"] == "[MemoryMaster recall]\nexplain \ufffd recall"
    assert not would_deliver(data, context, state_dir=tmp_path, now=11)
    assert not deliver(data, context, out.append, state_dir=tmp_path, now=11)
    reset_session(data["session_id"], state_dir=tmp_path)
    assert would_deliver(data, context, state_dir=tmp_path, now=12)


def test_deliver_claim_text_with_lone_surrogate_from_json(tmp_path):
    claim = json.loads(r'{"text":"SQLite \udc8f uses WAL; \u00f1 \ud83d\ude80"}')
    block = recall_block(claim["text"])
    assert block == "[MemoryMaster recall]\nSQLite \ufffd uses WAL; \u00f1 \U0001f680"
    block.encode("utf-8")  # strict output streams must accept the block itself too
    out = []
    # Direct callers of deliver() need the same boundary protection.
    assert deliver({"session_id": "claim"}, claim["text"], out.append, state_dir=tmp_path)
    assert json.loads(out[0])["hookSpecificOutput"]["additionalContext"] == block.split("\n", 1)[1]
