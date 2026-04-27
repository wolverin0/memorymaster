"""Tests for v3.9.0 F3 — cwd-from-transcript scope derivation (MemPalace v3.3.3 pattern)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.scope_utils import (
    cwd_from_transcript,
    scope_from_cwd,
    scope_from_transcript,
)


def test_scope_from_cwd_basic(tmp_path):
    assert scope_from_cwd(tmp_path / "memorymaster") == "project:memorymaster"


def test_scope_from_cwd_lowercases_and_dashes(tmp_path):
    assert scope_from_cwd(tmp_path / "My Project Name") == "project:my-project-name"


def test_scope_from_cwd_returns_global_for_empty():
    assert scope_from_cwd(None) == "global"
    assert scope_from_cwd("") == "global"


def test_scope_from_cwd_returns_global_for_nameless_root():
    """Root-only paths like ``/`` have empty .name → fall back to global."""
    assert scope_from_cwd("/") == "global"


def test_cwd_from_transcript_first_record(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"cwd": "/path/to/myproject", "role": "user"}) + "\n",
        encoding="utf-8",
    )
    assert cwd_from_transcript(transcript) == "/path/to/myproject"


def test_cwd_from_transcript_skips_records_without_cwd(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"role": "system"}),
                "",  # blank line
                "not-json-garbage",
                json.dumps({"role": "user"}),  # no cwd
                json.dumps({"cwd": "/found/it", "role": "assistant"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert cwd_from_transcript(transcript) == "/found/it"


def test_cwd_from_transcript_missing_file(tmp_path):
    assert cwd_from_transcript(tmp_path / "does-not-exist.jsonl") is None


def test_cwd_from_transcript_none_arg():
    assert cwd_from_transcript(None) is None


def test_cwd_from_transcript_empty_file(tmp_path):
    transcript = tmp_path / "empty.jsonl"
    transcript.write_text("", encoding="utf-8")
    assert cwd_from_transcript(transcript) is None


def test_cwd_from_transcript_no_cwd_anywhere(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"role": "user"}) + "\n" + json.dumps({"role": "assistant"}) + "\n",
        encoding="utf-8",
    )
    assert cwd_from_transcript(transcript) is None


def test_scope_from_transcript_uses_transcript_first(tmp_path):
    """When the transcript HAS a cwd, the fallback_cwd is ignored."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"cwd": "/path/to/authoritative", "role": "user"}) + "\n",
        encoding="utf-8",
    )
    out = scope_from_transcript(transcript, fallback_cwd="/wrong/path")
    assert out == "project:authoritative"


def test_scope_from_transcript_falls_back_to_arg(tmp_path):
    """When the transcript has no cwd, the fallback_cwd wins."""
    transcript = tmp_path / "no-cwd.jsonl"
    transcript.write_text(json.dumps({"role": "user"}) + "\n", encoding="utf-8")
    out = scope_from_transcript(transcript, fallback_cwd="/fallback/myapp")
    assert out == "project:myapp"


def test_scope_from_transcript_falls_back_to_global(tmp_path):
    transcript = tmp_path / "no-cwd.jsonl"
    transcript.write_text(json.dumps({"role": "user"}) + "\n", encoding="utf-8")
    assert scope_from_transcript(transcript, fallback_cwd=None) == "global"


def test_scope_from_transcript_handles_missing_transcript(tmp_path):
    """Missing file → None from cwd_from_transcript → fallback path used."""
    out = scope_from_transcript(tmp_path / "missing.jsonl", fallback_cwd="/x/myapp")
    assert out == "project:myapp"
