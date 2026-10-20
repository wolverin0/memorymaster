"""Offline format replay over disposable transcripts (T-1632); never the real home or database."""
import json

from datetime import datetime, timezone

from scripts.replay_recall_formats import replay

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
STAMP = "2026-10-12T12:00:00Z"


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def hook(context):
    return {"type": "attachment", "attachment": {
        "type": "hook_additional_context", "hookEvent": "UserPromptSubmit", "content": [context]}}


def setup_review(tmp_path, rows):
    home = tmp_path / "home"
    mapping = tmp_path / "lanes.json"
    mapping.write_text(json.dumps({"lanes": [{"lane": "demo", "repos": ["demo"]}]}))
    path = home / ".claude/projects/G---Py-Apps-demo/session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps({"timestamp": STAMP, **row}) + chr(10) for row in rows), encoding="utf-8")
    return home, mapping

BLOCK = ("[MemoryMaster recall]\n# Memory Context\nWhen a memory below informs your reply, cite its [mm-id].\n\n"
         "- [mm-a1] Deploy the billing worker with the staging flag first\n"
         "- [mm-b2] Use pytest fixtures for temporary databases")


def test_replay_counts_each_mode_and_emits_no_text(tmp_path):
    home, mapping = setup_review(tmp_path, [
        user("how do I deploy the billing worker?"), hook(BLOCK), assistant("Following [mm-a1]."),
        user("what is the weather today"), hook(BLOCK), assistant("No idea."),
    ])
    result = replay(home, mapping, days=7, now=NOW)
    modes = result["total"]["modes"]
    assert modes["current"]["prompts_with_injection"] == 2
    assert modes["current"]["cited_prompts_still_injected"] == 1
    assert modes["header"]["prompts_with_injection"] == 2 and modes["header"]["injected_claims"] == 4
    assert modes["gated"]["prompts_with_injection"] == 1  # the weather prompt shares nothing with the claims
    assert modes["gated"]["cited_prompts_still_injected"] == 1
    assert result["total"]["baseline"]["relevance"]["no_shared_token"] == 1
    assert result["official_script_agreement"]["injected_claims"] == [4, 4]
    emitted = json.dumps(result)
    assert "billing worker" not in emitted and "weather" not in emitted  # counts and ids only


def test_replay_ignores_the_appended_look_ahead_briefing(tmp_path):
    """The installed hook appends a briefing to the same payload; it is not a claim and must not be reshaped."""
    home, mapping = setup_review(tmp_path, [
        user("how do I deploy the billing worker?"),
        hook(BLOCK + "\n\n[Look-ahead briefing]\n- [mm-zz] briefing bullet that is not a recall claim"),
    ])
    result = replay(home, mapping, days=7, now=NOW)
    assert result["total"]["modes"]["header"]["injected_claims"] == 2
    assert result["total"]["modes"]["current"]["injected_claims"] == 2


def test_replay_counts_only_recall_claims_in_every_metric(tmp_path):
    """Review fix: ids from the appended look-ahead briefing are not injected recall claims."""
    home, mapping = setup_review(tmp_path, [
        user("how do I deploy the billing worker?"),
        hook(BLOCK + "\n\n[Look-ahead briefing]\n- [mm-zz] briefing bullet that is not a recall claim"),
        assistant("Using [mm-zz] and [mm-a1]."),
    ])
    result = replay(home, mapping, days=7, now=NOW)
    day = next(iter(result["per_day"].values()))
    assert day["injected_claims"] == 2 and day["cited_claims"] == 1
    assert result["total"]["modes"]["current"]["cited_claims_total"] == 1
    assert result["total"]["strict_prompts_with_citation"] == 1
    assert all(item["id"] != "mm-zz" for item in result["top10_injected_never_cited"])
    assert result["recall_only_totals"] == {"prompts_with_injection": 1, "injected_claims": 2, "cited_claims": 1}
