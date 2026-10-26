"""Session idle handoff respects capture activation and fails silently."""
import io
import json

import pytest

from memorymaster.dreaming import capture
from memorymaster.surfaces import hook_capture


def test_idle_dispatches_to_existing_pipeline(tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(tmp_path / "queue.db"))
    monkeypatch.setenv("MEMORYMASTER_DREAM_ENABLED", "1")
    payload = {"session_id": "ses_1", "cwd": str(tmp_path), "data_dir": str(tmp_path / "opencode")}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    def handoff(data, **kwargs):
        seen.update(payload=data, provider=kwargs["provider"], path=kwargs["ledger"].db_path)

    monkeypatch.setattr(capture, "capture_hook_payload", handoff)
    assert hook_capture.main(["--cli", "opencode"]) == 0
    assert seen == {"payload": payload, "provider": "opencode", "path": tmp_path / "queue.db"}
    assert capsys.readouterr() == ("", "")


def test_disabled_capture_never_opens_queue(monkeypatch, capsys):
    monkeypatch.setenv("MEMORYMASTER_DREAM_ENABLED", "0")
    monkeypatch.setattr(capture, "capture_hook_payload", lambda *a, **k: pytest.fail("disabled"))
    assert hook_capture.main(["--cli", "opencode"]) == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("payload", ["broken", "[]", "{}"])
def test_capture_errors_are_silent(monkeypatch, capsys, payload):
    monkeypatch.delenv("MEMORYMASTER_DREAM_ENABLED", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    monkeypatch.setattr(capture, "capture_hook_payload", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert hook_capture.main(["--cli", "opencode"]) == 0
    assert capsys.readouterr() == ("", "")
