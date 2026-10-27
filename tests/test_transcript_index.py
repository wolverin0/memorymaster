"""T-1860: opt-in raw-transcript fallback index (synthetic JSONL, tmp dirs only)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from memorymaster.recall import transcript_index as ti
from memorymaster.surfaces.cli import main as cli_main


def _rec(role: str, text: str, ts: str, cwd: str = "/work/alpha-app", **extra) -> str:
    body = {"type": role, "timestamp": ts, "cwd": cwd, "message": {"role": role, "content": [{"type": "text", "text": text}]}}
    body.update(extra)
    return json.dumps(body)


def _write(path: Path, lines: list[str], *, newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if newline else ""), encoding="utf-8")


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "projects"
    _write(root / "proj-a" / "s1.jsonl", [
        _rec("user", "Why does the nightly rollup fail with the quokka timeout?", "2026-03-01T10:00:00Z"),
        _rec("assistant", "The quokka timeout comes from the rollup lock being held too long.", "2026-03-01T10:00:05Z"),
        _rec("user", "tiny", "2026-03-01T10:01:00Z"),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "x"}]}}),
        _rec("assistant", "sidechain quokka noise that must be skipped entirely", "2026-03-01T10:02:00Z", isSidechain=True),
    ])
    _write(root / "proj-b" / "s2.jsonl", [
        _rec("assistant", "A newer note: the quokka timeout was fixed by raising the lock budget.", "2026-09-15T08:30:00Z"),
    ])
    return root, tmp_path / "idx" / "transcripts.db"


def test_index_then_search_returns_dated_cited_fragments(tmp_path):
    root, idx = _setup(tmp_path)
    stats = ti.index_transcripts(root, idx)
    assert stats["files_indexed"] == 2 and stats["fragments_added"] == 2

    hits = ti.search("quokka timeout", idx, limit=5)
    assert len(hits) == 2
    for h in hits:
        assert len(h.date) == 20 and h.date.endswith("Z")
        assert len(h.session_id) == 16
        assert h.scope.startswith("project:")
        assert h.first_line <= h.last_line
    assert "sidechain" not in " ".join(h.excerpt for h in hits)
    assert ti.session_id_for(root, root / "proj-a" / "s1.jsonl") in {h.session_id for h in hits}


def test_equal_relevance_prefers_newest_but_relevance_wins(tmp_path):
    root = tmp_path / "projects"
    same = "The migration checklist mentions zebrafish deployment ordering clearly."
    _write(root / "p" / "old.jsonl", [_rec("assistant", same, "2026-01-01T00:00:00Z")])
    _write(root / "p" / "new.jsonl", [_rec("assistant", same, "2026-08-01T00:00:00Z")])
    _write(root / "p" / "weak.jsonl", [_rec("assistant", "zebrafish " + "filler words go here " * 40, "2026-10-01T00:00:00Z")])
    idx = tmp_path / "t.db"
    ti.index_transcripts(root, idx)
    hits = ti.search("zebrafish deployment ordering checklist", idx, limit=3)
    assert [h.date[:10] for h in hits[:2]] == ["2026-08-01", "2026-01-01"]
    assert hits[-1].date.startswith("2026-10-01")  # newest but much weaker: not promoted


def test_incremental_skips_unchanged_appends_and_rebuilds_on_shrink(tmp_path):
    root, idx = _setup(tmp_path)
    assert ti.index_transcripts(root, idx)["files_indexed"] == 2
    again = ti.index_transcripts(root, idx)
    assert again["files_indexed"] == 0 and again["files_skipped"] == 2

    target = root / "proj-b" / "s2.jsonl"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(_rec("user", "Follow up about the okapi cache eviction policy please.", "2026-09-16T09:00:00Z") + "\n")
    inc = ti.index_transcripts(root, idx)
    assert inc["files_indexed"] == 1 and inc["fragments_added"] == 1
    hit = ti.search("okapi eviction", idx)[0]
    assert hit.first_line == 1  # absolute line number survives the incremental parse
    assert len(ti.search("quokka timeout", idx)) == 2  # old content not duplicated

    _write(target, [_rec("user", "Rewritten session about the axolotl scheduler.", "2026-09-17T09:00:00Z")])
    assert ti.index_transcripts(root, idx)["files_indexed"] == 1
    assert ti.search("okapi", idx) == []
    assert len(ti.search("axolotl scheduler", idx)) == 1
    with sqlite3.connect(idx) as con:  # raises if the FTS shadow tables drifted from the content table
        con.execute("INSERT INTO fragments_fts(fragments_fts, rank) VALUES('integrity-check', 1)")


def test_partial_trailing_line_is_retried_next_run(tmp_path):
    root = tmp_path / "projects"
    path = root / "p" / "s.jsonl"
    full = _rec("assistant", "Complete message about the narwhal gateway retry budget.", "2026-02-02T00:00:00Z")
    path.parent.mkdir(parents=True)
    path.write_text(full + "\n" + full[: len(full) // 2], encoding="utf-8")
    idx = tmp_path / "t.db"
    assert ti.index_transcripts(root, idx)["fragments_added"] == 1
    path.write_text(full + "\n" + _rec("user", "Second message about the narwhal gateway latency.", "2026-02-03T00:00:00Z") + "\n", encoding="utf-8")
    assert ti.index_transcripts(root, idx)["fragments_added"] == 1


def test_secrets_and_home_paths_are_redacted_in_output(tmp_path):
    root = tmp_path / "projects"
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
    home = "C:\\" + "Users" + "\\someone"  # synthetic Windows home path
    _write(root / "p" / "s.jsonl", [_rec(
        "user", f"The pangolin export used key {secret} from {home}\\notes.txt yesterday.",
        "2026-04-04T00:00:00Z")])
    idx = tmp_path / "t.db"
    ti.index_transcripts(root, idx)
    out = ti.search("pangolin export", idx)[0].excerpt
    assert secret not in out and "someone" not in out and "[REDACTED" in out


def test_search_filters_and_missing_index(tmp_path):
    root, idx = _setup(tmp_path)
    assert ti.search("quokka", tmp_path / "nope.db") == []
    ti.index_transcripts(root, idx)
    assert len(ti.search("quokka", idx, since="2026-06-01")) == 1
    scope = ti.search("quokka", idx)[0].scope
    assert ti.search("quokka", idx, scope=scope)
    assert ti.search("quokka", idx, scope="project:other") == []
    assert ti.search("-:* \"\"", idx) == []  # operator-only queries never raise


def test_cli_roundtrip_never_needs_claims_db(tmp_path, capsys, monkeypatch):
    root, idx = _setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli_main(["--json", "transcript-index", "--root", str(root), "--index", str(idx)]) == 0
    assert json.loads(capsys.readouterr().out)["files_indexed"] == 2
    assert cli_main(["--json", "transcript-search", "quokka timeout", "--index", str(idx), "--limit", "1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] and payload["rows"] == 1 and payload["hits"][0]["date"].endswith("Z")
    assert not list(tmp_path.glob("*.db"))  # no memorymaster.db was created in cwd
