"""Session deduplication accepts text decoded from JSON, including surrogates."""
from memorymaster.recall import hook_policy


def test_lone_surrogate_session_preserves_seen_ids_and_reset():
    session = "session\udc8f"
    hook_policy.remember(session, [3, 9], now=10)
    assert hook_policy.seen_ids(session, now=11) == frozenset({3, 9})
    hook_policy.forget(session)
    assert hook_policy.seen_ids(session, now=12) == frozenset()


def test_stage_fragment_adds_to_the_staged_turn_and_only_to_an_existing_one(tmp_path, monkeypatch):
    monkeypatch.setenv(hook_policy.SEEN_DIR_ENV, str(tmp_path))
    hook_policy.stage_fragment("s", "t1", -5, "</tag>")  # nothing staged: nothing created
    assert hook_policy.take_turn("s", "t1") is None
    hook_policy.stage_turn("s", "t2", {"turn": "t2", "packed": []}, now=100)
    hook_policy.stage_fragment("s", "t2", -5, "</tag>")
    turn = hook_policy.take_turn("s", "t2", now=101)
    assert turn["fragment"] == {"key": -5, "tag": "</tag>"} and turn["packed"] == []
