"""T-2277: the transcript index reads Codex, agy and OpenCode sessions, not only Claude's (disposable fixtures)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
from test_dreaming_agy_source import _db, _metadata, _row, _rows
from test_opencode_capture import _fixture as _opencode_fixture
from test_opencode_capture import _store as _opencode_store

from memorymaster.core.scope_utils import scope_from_cwd
from memorymaster.recall import transcript_index as ti
from memorymaster.recall import transcript_sources as ts

UUID = "12345678-1234-1234-1234-123456789abc"
WORDS = {"claude": "quokkaclaude", "codex": "quokkacodex", "agy": "quokkaagy", "opencode": "quokkaopencode"}


def _say(word: str) -> str:
    return f"We decided the {word} retention window is final, thirty days."


@pytest.fixture()
def home(tmp_path, monkeypatch):
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path / "home"))
    for name in ("XDG_DATA_HOME", "OPENCODE_DB", ti.ROOT_ENV, *(f"MEMORYMASTER_TRANSCRIPT_{k}_{c.upper()}"
                                                                for k in ("SOURCE", "ROOT") for c in ts.CLIS)):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(ti.INDEX_ENV, str(tmp_path / "idx" / "transcripts.db"))
    base = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    roots = ts.roots_under(base)
    (roots["claude"] / "p").mkdir(parents=True)
    (roots["claude"] / "p" / "c.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-11-01T10:00:00Z", "cwd": str(work),
        "message": {"role": "assistant", "content": [{"type": "text", "text": _say(WORDS["claude"])}]}}) + "\n",
        encoding="utf-8")
    codex = roots["codex"] / "2026" / "11" / f"rollout-2026-11-02T10-00-00-{UUID}.jsonl"
    codex.parent.mkdir(parents=True)
    codex.write_text("\n".join(json.dumps(r) for r in (
        {"timestamp": "2026-11-02T10:00:00Z", "type": "session_meta", "payload": {"id": UUID, "cwd": str(work)}},
        {"timestamp": "2026-11-02T10:00:01Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": _say(WORDS["codex"])}]}},
        {"timestamp": "2026-11-02T10:00:02Z", "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "x", "output": "tool output must stay out of the index"}},
    )) + "\n", encoding="utf-8")
    logs = roots["agy"] / "brain" / UUID / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    _rows(logs / "transcript.jsonl", [_row(0, "USER_INPUT", content=_say(WORDS["agy"])),
                                      _row(1, content="Noted, keeping the thirty day window as decided.")])
    (roots["agy"] / "conversations").mkdir()
    _db(roots["agy"] / "conversations" / f"{UUID}.db", [_metadata(str(work))])
    _workspace, document, _ = _opencode_fixture(tmp_path)
    document["info"]["directory"] = str(work)
    document["messages"][0]["parts"][0]["text"] = _say(WORDS["opencode"])
    roots["opencode"].parent.mkdir(parents=True)
    _opencode_store(roots["opencode"], document, "v1")
    return work


def _fragments(cli: str | None = None) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(ti.default_index_path())) as conn:
        return conn.execute("SELECT cli, text FROM fragments" + (f" WHERE cli = '{cli}'" if cli else "")).fetchall()


def test_every_cli_is_indexed_with_its_cli_session_and_roles(home) -> None:
    ti.index_transcripts()
    scope = scope_from_cwd(str(home))
    for cli, word in WORDS.items():
        (hit,) = ti.search(word, scope=scope)
        assert (hit.cli, hit.scope) == (cli, scope) and word in hit.excerpt
    assert {cli for cli, _ in _fragments()} == set(ts.CLIS)
    assert all(text.startswith(("user: ", "assistant: ")) for _, text in _fragments())
    assert _fragments("codex")[0][1].startswith("user: ")  # the Codex user turn keeps its role
    assert not any("tool output" in text for _, text in _fragments())  # tool traffic stays out


def test_coverage_counts_sessions_indexed_over_sessions_present(home) -> None:
    before = ti.coverage()
    assert {cli: (v["present"], v["indexed"]) for cli, v in before.items()} == {cli: (1, 0) for cli in ts.CLIS}
    ti.index_transcripts()
    assert ti.coverage() == {cli: {"present": 1, "indexed": 1, "searchable": 1, "coverage": 1.0} for cli in ts.CLIS}


def test_a_switched_off_source_is_neither_served_nor_kept(home, monkeypatch) -> None:
    ti.index_transcripts()
    scope = scope_from_cwd(str(home))
    monkeypatch.setenv("MEMORYMASTER_TRANSCRIPT_SOURCE_CODEX", "0")
    assert ti.search(WORDS["codex"], scope=scope) == []  # served no more, even before the next index run
    assert [h.cli for h in ti.search(WORDS["claude"], scope=scope)] == ["claude"]
    ti.index_transcripts()
    assert "codex" not in {cli for cli, _ in _fragments()}
    monkeypatch.delenv("MEMORYMASTER_TRANSCRIPT_SOURCE_CODEX")
    ti.index_transcripts()
    assert [h.cli for h in ti.search(WORDS["codex"], scope=scope)] == ["codex"]


def test_an_explicit_claude_root_leaves_the_other_sources_alone(home, tmp_path) -> None:
    ti.index_transcripts()
    ti.index_transcripts(tmp_path / "home" / ".claude" / "projects")
    assert {cli for cli, _ in _fragments()} == set(ts.CLIS)


def test_a_secret_stated_in_another_cli_is_redacted_before_it_is_stored(home, tmp_path) -> None:
    rollout = next((tmp_path / "home" / ".codex" / "sessions").rglob("*.jsonl"))
    secret = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    with rollout.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"timestamp": "2026-11-02T10:00:03Z", "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text",
                                                                  "text": f"The quokkacodex call used Authorization: {secret}"}]}}) + "\n")
    ti.index_transcripts()
    stored = " ".join(text for _, text in _fragments("codex"))
    assert "The quokkacodex call used" in stored and "eyJhbGciOiJIUzI1NiJ9" not in stored


def test_delivery_metrics_reports_index_coverage_per_cli(home, tmp_path) -> None:
    from scripts import delivery_metrics as dm

    ti.index_transcripts(index_path=tmp_path / "home" / ".memorymaster" / "transcripts.db")
    report = dm.collect(tmp_path / "no.db", tmp_path / "no2.db", tmp_path / "home", tmp_path / "x.jsonl")
    assert report["transcript_index_coverage"] == {
        cli: {"present": 1, "indexed": 1, "searchable": 1, "coverage": 1.0} for cli in ts.CLIS}
