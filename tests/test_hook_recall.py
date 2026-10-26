"""OpenCode uses the same governed hook core as Claude, with plain-text output."""
import io
import json

import pytest

from memorymaster.surfaces import hook_recall


def test_opencode_delegates_query_workspace_and_budget_to_shared_core(monkeypatch, capsys):
    seen = {}

    def recall(query, data, **kwargs):
        seen.update(query=query, data=data, **kwargs)
        return "# Memory Context\n- [mm-42] use SQLite", "shared"

    monkeypatch.setattr(hook_recall, "hook_recall", recall)
    monkeypatch.setattr(hook_recall, "format_mode", lambda: "current")
    monkeypatch.setenv("MEMORYMASTER_DEFAULT_DB", "fixture.db")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "prompt": "Why did we choose SQLite?", "cwd": "/work/project", "session_id": "ses_1",
    })))
    assert hook_recall.main(["--cli", "opencode"]) == 0
    assert capsys.readouterr().out == "[MemoryMaster recall]\n# Memory Context\n- [mm-42] use SQLite"
    assert seen == {"query": "Why did we choose SQLite?", "data": {
        "prompt": "Why did we choose SQLite?", "cwd": "/work/project", "session_id": "opencode:ses_1",
    }, "db_path": "fixture.db", "timeout": 2.5}


@pytest.mark.parametrize("payload", ["[]", "broken", "{}", '{"prompt": 7}',
                                          '{"prompt":"ok"}', '{"prompt":"<task-notification>done"}'])
def test_bad_or_automated_inputs_are_silent(monkeypatch, capsys, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    monkeypatch.setattr(hook_recall, "hook_recall", lambda *a, **k: pytest.fail("must skip recall"))
    assert hook_recall.main(["--cli", "opencode"]) == 0
    assert capsys.readouterr() == ("", "")


def test_recall_failure_is_silent(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"prompt":"Why is this failing?","cwd":"/work"}'))
    monkeypatch.setattr(hook_recall, "hook_recall", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert hook_recall.main(["--cli", "opencode"]) == 0
    assert capsys.readouterr() == ("", "")
