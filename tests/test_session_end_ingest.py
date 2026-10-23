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


def test_generic_payload_model_turn_preserves_legacy_distillation(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps({"payload": {"role": "model", "content": "A durable authored finding about SQLite transaction boundaries."}}), encoding="utf-8")
    assert "durable authored" in _assistant_text(str(path))


def test_cli_records_explicit_end_before_distillation_failure(tmp_path, monkeypatch):
    from memorymaster.profile import project_runtime
    from memorymaster.surfaces import session_end_ingest as ingest

    seen = []
    monkeypatch.setattr(project_runtime, "record_session_end", lambda data: seen.append(data))
    monkeypatch.setattr(ingest, "run", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("fixture failure")))
    assert ingest.main(["--db", str(tmp_path / "fixture.db"), "--transcript", str(tmp_path / "missing.jsonl"),
                        "--cwd", str(tmp_path), "--session-id", "explicit", "--end-at", "2026-10-22T09:00:00Z"]) == 1
    assert seen == [{"cwd": str(tmp_path), "session_id": "explicit", "end_at": "2026-10-22T09:00:00Z"}]
    seen.clear()
    monkeypatch.setattr(ingest, "run", lambda *args, **kwargs: 0)
    assert ingest.main(["--db", str(tmp_path / "fixture.db"), "--transcript", "unused"]) == 0
    assert not seen
