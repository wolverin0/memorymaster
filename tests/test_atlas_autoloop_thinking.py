"""The batched extractor's Gemini call must use the shared thinking guard (T-2077a, L21)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "atlas_autoloop.py"


@pytest.fixture
def autoloop(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location("atlas_autoloop_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("model,expected", [
    ("gemini-3.1-flash-lite", {"thinkingLevel": "minimal"}),
    ("gemini-3-pro", None),  # the old `"gemini-3" in MODEL` test sent MINIMAL to Pro
    ("gemini-2.5-flash-lite", {"thinkingBudget": 0}),
])
def test_call_uses_the_shared_thinking_guard(autoloop, monkeypatch, model, expected):
    sent = {}

    class _Resp:
        def read(self):
            return b'{"candidates": [{"content": {"parts": [{"text": "[]"}]}}]}'

    def fake_urlopen(req, timeout=0):
        sent["gen"] = json.loads(req.data)["generationConfig"]
        return _Resp()

    monkeypatch.setattr(autoloop, "MODEL", model)
    monkeypatch.setattr(autoloop.urllib.request, "urlopen", fake_urlopen)
    autoloop._call("prompt")
    assert sent["gen"].get("thinkingConfig") == expected
