import json

from memorymaster.surfaces.session_end_ingest import _assistant_text


def test_generated_turn_filtered_before_distillation(tmp_path):
    from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER
    raw = f"{GENERATED_OPENER}\n" + "excluded fact " * 100 + f"\n{GENERATED_CLOSER}\n" + "A newly authored durable decision about transactions. " * 3
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps({"role": "assistant", "content": raw}), encoding="utf-8")
    result = _assistant_text(str(path))
    assert "excluded fact" not in result
    assert "newly authored" in result


def test_chronological_tool_state_precedes_last_two_hundred_slice(tmp_path):
    rows = [{"role": "assistant", "content": [{"type": "tool_use", "id": "recall", "name": "mcp__memorymaster__query_memory"}]}]
    rows += [{"role": "assistant", "content": "Unmarked recalled echo should never be distilled into a new claim."}] * 205
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    assert _assistant_text(str(path)) == ""
