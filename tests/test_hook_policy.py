"""Session deduplication accepts text decoded from JSON, including surrogates."""
from memorymaster.recall import hook_policy


def test_lone_surrogate_session_preserves_seen_ids_and_reset():
    session = "session\udc8f"
    hook_policy.remember(session, [3, 9], now=10)
    assert hook_policy.seen_ids(session, now=11) == frozenset({3, 9})
    hook_policy.forget(session)
    assert hook_policy.seen_ids(session, now=12) == frozenset()
