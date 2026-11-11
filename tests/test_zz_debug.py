from test_hook_transcript_fallback import world  # noqa: F401
from test_hook_transcript_fallback import _two_claims
from memorymaster.recall import context_hook as c


def test_dbg(world, monkeypatch):
    world.transcripts(("assistant", "The router firmware version field belongs in the deploy checklist", "2026-08-01T00:00:00Z", world.cwd))
    _two_claims(world)
    orig = c._evidence_fragment_key
    def spy(rendered, top, query, session):
        new = c._content_tokens(top.excerpt) - c._content_tokens(c._MM_ID.sub("", rendered)) - c._content_tokens(query)
        raise AssertionError((repr(top.excerpt), repr(query), sorted(new), repr(rendered)))
    monkeypatch.setattr(c, "_evidence_fragment_key", spy)
    world.ask()
