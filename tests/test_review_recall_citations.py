"""Disposable transcript contracts for the read-only utility review."""

import json
from datetime import datetime, timezone

from scripts.review_recall_citations import review


NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
STAMP = "2026-10-12T12:00:00Z"
CONTEXT = "[MemoryMaster recall]\n# Memory Context\n- [mm-a1] alpha\n- [mm-b2] beta\n- [mm-c3] gamma"


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"timestamp": STAMP, **row}) + "\n" for row in rows), encoding="utf-8")


def user(text, **extra):
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def assistant(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def hook(context=CONTEXT, event="UserPromptSubmit"):
    return {"type": "attachment", "attachment": {
        "type": "hook_additional_context", "hookEvent": event, "content": [context],
    }}


def setup_review(tmp_path, rows):
    home = tmp_path / "home"
    mapping = tmp_path / "lanes.json"
    mapping.write_text(json.dumps({"lanes": [{"lane": "demo", "repos": ["demo"]},
                                              {"lane": "silent", "repos": ["silent"]}]}))
    write_rows(home / ".claude/projects/G---Py-Apps-demo/session.jsonl", rows)
    return home, mapping


def test_same_turn_citations_and_next_prompt_boundary(tmp_path):
    home, mapping = setup_review(tmp_path, [
        user("first prompt"), hook(),
        assistant("Uses [mm-a1], repeated [mm-a1], and unrelated [mm-else]."),
        user([{ "type": "tool_result", "content": "[mm-b2]"}]),
        assistant("Still [mm-a1]."),
        user("next real prompt"), assistant("Late [mm-b2] [mm-c3]."),
    ])
    result = review(home, mapping, days=7, now=NOW)
    lane = result["lanes"]["demo"]
    assert lane["prompts_with_injection"] == 1
    assert lane["injected_claims"] == 3
    assert lane["cited_claims"] == 1
    assert lane["cited_injected_rate"] == 1 / 3
    assert lane["prompts_with_citation"] == 1
    assert lane["most_injected_never_cited"] == [{"id": "mm-b2", "injected": 1}, {"id": "mm-c3", "injected": 1}]
    assert "silent" in result["lanes_without_observed_injection"]


def test_exclude_quotes_tools_thinking_compaction_and_other_hooks(tmp_path):
    home, mapping = setup_review(tmp_path, [
        user("quoted example:\n" + CONTEXT),
        hook(event="PreToolUse"), assistant("[mm-a1]"),
        user("actual prompt"), hook(),
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "[mm-a1]"},
            {"type": "tool_use", "input": {"text": "[mm-b2]"}},
        ]}},
        user("summary [mm-c3]", isCompactSummary=True), assistant("[mm-c3]"),
    ])
    result = review(home, mapping, days=7, now=NOW)
    assert result["total"]["injected_claims"] == 3
    assert result["total"]["cited_claims"] == 0
    assert result["diagnostics"]["compaction_boundaries"] == 1


def test_subagents_cutoff_dedup_dense_and_no_private_output(tmp_path):
    home, mapping = setup_review(tmp_path, [user("secret 192.0.2.1"), hook(), assistant("[mm-a1]")])
    sub = home / ".claude/projects/G---Py-Apps-demo/session/subagents/agent.jsonl"
    write_rows(sub, [user("sub prompt"), hook(), assistant("[mm-b2]")])
    write_rows(home / ".claude/projects/G---Py-Apps-demo/old.jsonl", [
        user("old", timestamp="2026-10-01T12:00:00Z"),
        {**hook(), "timestamp": "2026-10-01T12:00:01Z"}, assistant("[mm-c3]"),
    ])
    dense = home / ".memorymaster/metrics/recall-dense.jsonl"
    write_rows(dense, [{"ts": STAMP, "scope": "project:demo", "injected_ids": [1, 2, 3]}])
    with dense.open("a") as stream:
        stream.write("malformed\n")
    result = review(home, mapping, days=7, now=NOW)
    assert result["total"]["injected_claims"] == 3
    assert result["subagents"]["total"]["injected_claims"] == 3
    assert result["subagents"]["total"]["cited_claims"] == 1
    assert result["dense_cross_check"]["lanes"]["demo"]["injected_claims"] == 3
    assert result["diagnostics"]["malformed_dense_lines"] == 1
    output = json.dumps(result)
    assert str(home) not in output
    assert "secret" not in output
    assert "192.0.2.1" not in output


def test_codex_probe_reports_absence_of_injection(tmp_path):
    home, mapping = setup_review(tmp_path, [])
    def message(role, text):
        return {"type": "response_item", "payload": {"type": "message", "role": role,
                "content": [{"type": "input_text", "text": text}]}}
    write_rows(home / ".codex/sessions/2026/10/12/session.jsonl", [
        message("user", "brief quoting [MemoryMaster recall] mid-sentence"),
        {"type": "event_msg", "payload": {"type": "item_completed", "text": CONTEXT}},
    ])
    probe = review(home, mapping, days=7, now=NOW)["providers"]["codex"]
    assert probe == {"files_scanned": 1, "injection_events": 0, "injection_format_observed": False}


def test_strict_bracket_vs_lenient_bare_tokens(tmp_path):
    home, mapping = setup_review(tmp_path, [
        user("p"), hook(), assistant("Per **mm-b2**, not mm-a10 or [mm-a1, mm-9f]."),
    ])
    lane = review(home, mapping, days=7, now=NOW)["lanes"]["demo"]
    assert (lane["cited_claims"], lane["cited_claims_lenient"]) == (1, 2)
    assert lane["cited_injected_rate_lenient"] == 2 / 3


def test_resumed_copy_dedupes_and_unions_citations(tmp_path):
    home, mapping = setup_review(tmp_path, [])
    first = [user("p", uuid="u1"), hook(), assistant("no cite")]
    second = [user("p", uuid="u1"), hook(), assistant("[mm-c3]")]
    write_rows(home / ".claude/projects/G---Py-Apps-demo/a.jsonl", first)
    write_rows(home / ".claude/projects/G---Py-Apps-demo/b.jsonl", second)
    lane = review(home, mapping, days=7, now=NOW)["lanes"]["demo"]
    assert (lane["prompts_with_injection"], lane["injected_claims"], lane["cited_claims"]) == (1, 3, 1)


def test_inline_sidechain_is_separate_from_main_turn(tmp_path):
    home, mapping = setup_review(tmp_path, [
        user("main"), hook(), {**user("side prompt"), "isSidechain": True},
        {**assistant("[mm-b2]"), "isSidechain": True}, assistant("[mm-a1]"),
    ])
    result = review(home, mapping, days=7, now=NOW)
    assert result["lanes"]["demo"]["cited_claims"] == 1
    assert result["subagents"]["total"]["injected_claims"] == 0


def test_lane_mapping_worktree_unmapped_and_external(tmp_path):
    home, mapping = setup_review(tmp_path, [])
    root = home / ".claude/projects"
    for name in ("G---OneDrive-Desktop-Py-Apps-demo--claude-worktrees-t1",
                 "G---OneDrive-Desktop-Py-Apps-stray",
                 "C--Users-someone-Documents"):
        write_rows(root / name / "s.jsonl", [user("p"), hook(), assistant("[mm-a1]")])
    lanes = review(home, mapping, days=7, now=NOW)["lanes"]
    assert set(lanes) == {"demo", "other:stray", "other:external"}
    assert all(lane["cited_claims"] == 1 for lane in lanes.values())


def test_hidden_worktree_dir_maps_to_repo_and_notification_sensitivity(tmp_path):
    home, mapping = setup_review(tmp_path, [])
    write_rows(home / ".claude/projects/G---Py-Apps--worktrees-demo-night/s.jsonl", [
        user("p"), hook(), user("<task-notification>done</task-notification>"), assistant("[mm-a1]"),
    ])
    result = review(home, mapping, days=7, now=NOW, sensitivity=True)
    assert result["lanes"]["demo"]["cited_claims"] == 0
    soft = result["sensitivity_task_notification_continues_turn"]["lanes"]["demo"]
    assert soft["cited_claims"] == 1


def test_instruction_split_empty_injection_and_silent_lane_activity(tmp_path):
    hinted = "[MemoryMaster recall]\n# Memory Context\nWhen a memory below informs your reply, cite its [mm-id].\n\n- [mm-a1] x"
    home, mapping = setup_review(tmp_path, [
        user("one"), hook(hinted), assistant("[mm-a1]"),
        user("two"), hook(), assistant("nothing"),
        user("three"), hook("[MemoryMaster recall]\n# Memory Context\n\n- plain one\n- plain two"),
    ])
    result = review(home, mapping, days=7, now=NOW)
    split = result["total_by_cite_instruction"]
    assert (split["instructed"]["injected_claims"], split["instructed"]["cited_claims"]) == (1, 1)
    assert (split["uninstructed"]["injected_claims"], split["uninstructed"]["cited_claims"]) == (3, 0)
    assert result["diagnostics"]["injections_without_ids"] == 1
    assert result["diagnostics"]["claims_in_injections_without_ids"] == 2
    assert result["lanes_without_observed_injection_prompts_total"] == {"silent": 0}


def persisted_stub(target, preview):
    return ("<persisted-output>\nOutput too large (10.2KB). Full output saved to: "
            f"{target}\n\nPreview (first 2KB):\n{preview}")


def test_persisted_output_payload_is_read_in_full(tmp_path):
    home, mapping = setup_review(tmp_path, [])
    full = home / ".claude/projects/G---Py-Apps-demo/s/tool-results/hook-1-additionalContext.txt"
    full.parent.mkdir(parents=True)
    full.write_text(CONTEXT, encoding="utf-8")
    preview = "[MemoryMaster recall]\n# Memory Context\n- [mm-a1] alpha\n- [mm-b2] be"  # cut mid-line
    write_rows(home / ".claude/projects/G---Py-Apps-demo/session.jsonl", [
        user("p"), hook(persisted_stub(full, preview)), assistant("[mm-c3]"),
    ])
    result = review(home, mapping, days=7, now=NOW)
    assert result["lanes"]["demo"]["injected_claims"] == 3
    assert result["lanes"]["demo"]["cited_claims"] == 1
    assert result["diagnostics"]["persisted_recall_full"] == 1


def test_persisted_output_outside_projects_falls_back_to_preview(tmp_path):
    home, mapping = setup_review(tmp_path, [])
    outside = tmp_path / "elsewhere.txt"
    outside.write_text(CONTEXT, encoding="utf-8")
    preview = "[MemoryMaster recall]\n# Memory Context\n- [mm-a1] alpha"
    write_rows(home / ".claude/projects/G---Py-Apps-demo/session.jsonl", [
        user("p"), hook(persisted_stub(outside, preview)),
        user("q"), hook(persisted_stub(outside, "<unrelated hook preview>")),
    ])
    result = review(home, mapping, days=7, now=NOW)
    assert result["lanes"]["demo"]["injected_claims"] == 1
    assert result["diagnostics"]["persisted_recall_preview_only"] == 1


def test_os_username_scope_is_scrubbed_and_diagnostics_are_windowed(tmp_path):
    mapping = tmp_path / "lanes.json"
    mapping.write_text(json.dumps({"lanes": []}))
    home = tmp_path / "alice"
    write_rows(home / ".memorymaster/metrics/recall-dense.jsonl", [
        {"ts": STAMP, "scope": "project:alice", "injected_ids": [1]},
    ])
    plain = "[MemoryMaster recall]\n# Memory Context\n\n- legacy bullet"
    write_rows(home / ".claude/projects/G---Py-Apps-old/session.jsonl", [
        {**user("p"), "timestamp": "2026-09-01T12:00:00Z"},
        {**hook(plain), "timestamp": "2026-09-01T12:00:01Z"},
        {**user("q"), "timestamp": "2026-09-01T12:00:02Z"},
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-09-01T12:00:03Z"},
    ])
    result = review(home, mapping, days=7, now=NOW)
    assert "other:user-home" in result["dense_cross_check"]["lanes"]
    assert "alice" not in json.dumps(result)
    assert result["diagnostics"]["injections_without_ids"] == 0
    assert result["diagnostics"]["compaction_boundaries"] == 0


def test_header_format_block_is_instructed_and_counted_per_format(tmp_path):
    """T-1632: the header A/B arm carries its own cite hint; the review must not read it as uninstructed."""
    header_block = ("[MemoryMaster recall]\n# Decisions you made earlier (cite [mm-id] when you rely on one)\n"
                    "Ranked by relevance to your active task, then to this prompt.\n\n- [mm-a1] alpha")
    home, mapping = setup_review(tmp_path, [
        user("first"), hook(header_block), assistant("Relying on [mm-a1]."),
        user("second"), hook(),
    ])
    result = review(home, mapping, days=7, now=NOW)
    assert result["total"]["prompts_header_format"] == 1
    split = result["total_by_cite_instruction"]
    assert split["instructed"]["prompts_with_injection"] == 1 and split["instructed"]["prompts_with_citation"] == 1
