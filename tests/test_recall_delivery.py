"""Delivery policy: suppress repetition, never suppress recovery or real questions."""
import json

import pytest

from memorymaster.recall.delivery import deliver, is_automated, reset_session


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
