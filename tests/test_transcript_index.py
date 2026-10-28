"""T-1860: opt-in raw-transcript fallback index (synthetic JSONL, tmp dirs only)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
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


def test_headless_sdk_sessions_are_skipped_unless_requested(tmp_path):
    root = tmp_path / "projects"
    _write(root / "p" / "auto.jsonl", [
        _rec("user", "Answer only from the supplied context about the lemur gateway.", "2026-05-01T00:00:00Z",
             entrypoint="sdk-cli"),
        _rec("assistant", "The lemur gateway answer lives in the supplied context only.", "2026-05-01T00:00:01Z",
             entrypoint="sdk-cli"),
    ])
    _write(root / "p" / "human.jsonl", [
        _rec("user", "Interactive question about the lemur gateway timeout.", "2026-05-02T00:00:00Z",
             entrypoint="cli"),
    ])
    idx = tmp_path / "t.db"
    stats = ti.index_transcripts(root, idx)
    assert stats["files_automated"] == 1 and stats["files_indexed"] == 1
    assert [h.date[:10] for h in ti.search("lemur gateway", idx)] == ["2026-05-02"]
    assert ti.index_transcripts(root, idx)["files_skipped"] == 2  # automated file is remembered, not re-parsed

    full = tmp_path / "full.db"
    assert ti.index_transcripts(root, full, include_automated=True)["files_indexed"] == 2
    assert len(ti.search("lemur gateway", full)) == 2


def test_cli_search_survives_a_cp1252_console(tmp_path):
    root = tmp_path / "projects"
    _write(root / "p" / "s.jsonl", [_rec(
        "assistant", "Estado del servicio \u25cf apagado, config-batch-apply pendiente en el tablero.",
        "2026-06-06T00:00:00Z")])
    idx = tmp_path / "t.db"
    ti.index_transcripts(root, idx)
    repo = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    proc = subprocess.run(
        [sys.executable, "-m", "memorymaster.surfaces.cli", "transcript-search", "servicio apagado config-batch-apply",
         "--index", str(idx)],
        cwd=repo, env=env, capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout.decode("cp1252", "replace") + proc.stderr.decode("cp1252", "replace")
    out = proc.stdout.decode("cp1252")
    assert "2026-06-06" in out and "Estado del servicio" in out


def test_excerpt_is_centered_on_the_match_after_redaction(tmp_path):
    root = tmp_path / "projects"
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
    body = f"key {secret} " + " ".join(f"filler{i}" for i in range(150)) + " the zebrafish gateway latency spike " + "tail " * 30
    _write(root / "p" / "s.jsonl", [_rec("assistant", body, "2026-04-04T00:00:00Z")])
    idx = tmp_path / "t.db"
    ti.index_transcripts(root, idx)
    out = ti.search("zebrafish gateway latency", idx, excerpt_chars=200)[0].excerpt
    assert "zebrafish gateway latency" in out and secret not in out and "filler0 " not in out
    assert len(out) <= 210


def test_search_honours_a_deadline_without_raising(tmp_path):
    root, idx = _setup(tmp_path)
    ti.index_transcripts(root, idx)
    assert ti.search("quokka", idx, deadline_ms=5000)
    assert isinstance(ti.search("quokka", idx, deadline_ms=0.0001), list)  # an expired budget never raises


# --- T-1876 hardening: sanitise-before-persist, per-record scope, whole messages, rewrites, format marker ---

# credential-shaped values are assembled at runtime so no secret-looking literal sits in the source
SECRET_BODY = "".join(a + b for a, b in zip("abcdefghijkl", "ABCDEFGHIJKL"))
LEGACY_KEY = "sk" + "-" + "RAWKEY" * 4


def _fragment_texts(idx: Path) -> list[str]:
    with sqlite3.connect(idx) as con:
        return [row[0] for row in con.execute("SELECT text FROM fragments")]


def test_n1_a_credential_across_a_piece_boundary_is_never_persisted(tmp_path):
    root = tmp_path / "projects"
    pad = "word " * 357  # 1,785 chars: the key starts 15 chars before the 1,800 boundary and ends past it
    body = f"pangolin notes {pad}key {LEGACY_KEY[:3]}{SECRET_BODY} then more pangolin export words after it"
    _write(root / "p" / "s.jsonl", [_rec("assistant", body, "2026-04-04T00:00:00Z")])
    idx = tmp_path / "t.db"
    ti.index_transcripts(root, idx)
    stored = " ".join(_fragment_texts(idx))
    assert "pangolin export" in stored  # the message was indexed ...
    for part in (SECRET_BODY, SECRET_BODY[8:], SECRET_BODY[:8]):  # ... but no piece of the key reached the sidecar
        assert part not in stored
    with sqlite3.connect(idx) as con:
        hits = con.execute("SELECT count(*) FROM fragments_fts WHERE fragments_fts MATCH ?",
                           (f'"{SECRET_BODY[8:]}"',)).fetchone()[0]
    assert hits == 0
    assert SECRET_BODY[8:] not in " ".join(h.excerpt for h in ti.search("pangolin export notes", idx))


def test_n7_the_tail_of_a_long_message_is_indexed(tmp_path):
    root = tmp_path / "projects"
    body = "padding " * 1500 + " the router chosen firmware = 7.18.2 after the rollback"
    assert len(body) > 3 * ti.CHUNK_CHARS
    _write(root / "p" / "s.jsonl", [_rec("assistant", body, "2026-04-04T00:00:00Z")])
    idx = tmp_path / "t.db"
    ti.index_transcripts(root, idx)
    hits = ti.search("router chosen firmware rollback", idx)
    assert hits and "7.18.2" in hits[0].excerpt
    texts = _fragment_texts(idx)
    assert len(texts) > 3 and all(len(t) <= ti.CHUNK_CHARS + 20 for t in texts)


def test_n2_scope_follows_each_record_not_the_first_cwd(tmp_path):
    root = tmp_path / "projects"
    _write(root / "p" / "s.jsonl", [
        _rec("user", "Alpha decision: the zorblat firmware stays on the old release.", "2026-05-01T00:00:00Z",
             cwd="/work/alpha-app"),
        _rec("assistant", "Beta decision: the zorblat firmware moves to release 7.18.2.", "2026-05-01T00:00:05Z",
             cwd="/work/beta-app"),
        _rec("assistant", "Unattributable zorblat note without any cwd at all.", "2026-05-01T00:00:09Z", cwd=None),
    ])
    idx = tmp_path / "t.db"
    ti.index_transcripts(root, idx)
    alpha = " ".join(h.excerpt for h in ti.search("zorblat firmware", idx, scope="project:alpha-app"))
    beta = " ".join(h.excerpt for h in ti.search("zorblat firmware", idx, scope="project:beta-app"))
    assert "Alpha decision" in alpha and "7.18.2" not in alpha
    assert "7.18.2" in beta and "Alpha decision" not in beta
    everything = " ".join(h.excerpt for h in ti.search("zorblat", idx, limit=10))
    assert "Unattributable" not in everything  # no scope proven: never served


def test_n2_an_incremental_run_keeps_the_per_record_scope(tmp_path):
    root = tmp_path / "projects"
    path = root / "p" / "s.jsonl"
    _write(path, [_rec("user", "Alpha first note about the zorblat firmware schedule.", "2026-05-01T00:00:00Z",
                       cwd="/work/alpha-app")])
    idx = tmp_path / "t.db"
    ti.index_transcripts(root, idx)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_rec("assistant", "Beta later note about the zorblat firmware schedule.", "2026-05-02T00:00:00Z",
                      cwd="/work/beta-app") + "\n")
    ti.index_transcripts(root, idx)
    assert [h.excerpt for h in ti.search("zorblat", idx, scope="project:beta-app")] == [
        "assistant: Beta later note about the zorblat firmware schedule."]
    assert len(ti.search("zorblat", idx, scope="project:alpha-app")) == 1


def test_n5_deleted_and_rewritten_transcripts_leave_no_stale_fragments(tmp_path):
    root = tmp_path / "projects"
    gone, keep = root / "p" / "gone.jsonl", root / "p" / "keep.jsonl"
    _write(gone, [_rec("assistant", "The quokka decision was to keep the old scheduler forever.", "2026-05-01T00:00:00Z")])
    _write(keep, [_rec("assistant", "The wombat decision was to keep the old cache forever, fine.", "2026-05-01T00:00:00Z")])
    idx = tmp_path / "t.db"
    ti.index_transcripts(root, idx)
    assert ti.search("quokka scheduler", idx) and ti.search("wombat cache", idx)

    ti.index_transcripts(root, idx, max_files=0)  # a partial scan is not a deletion
    assert ti.search("quokka scheduler", idx)

    gone.unlink()
    stats = ti.index_transcripts(root, idx)
    assert ti.search("quokka scheduler", idx) == [] and stats["files_removed"] == 1
    assert ti.search("wombat cache", idx)  # the surviving transcript is untouched

    # same size, different bytes, newer mtime: the rewrite replaces the old statement
    before = keep.stat()
    keep.write_bytes(keep.read_bytes().replace(b"keep the old cache", b"drop the old cache"))
    assert keep.stat().st_size == before.st_size
    os.utime(keep, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
    ti.index_transcripts(root, idx)
    texts = " ".join(_fragment_texts(idx))
    assert "drop the old cache" in texts and "keep the old cache" not in texts


def test_n1_old_format_index_is_refused_then_rebuilt(tmp_path):
    root = tmp_path / "projects"
    _write(root / "p" / "s.jsonl", [_rec("assistant", "Fresh zebra message about the gateway deployment.",
                                         "2026-05-01T00:00:00Z")])
    idx = tmp_path / "t.db"
    assert ti.index_status(idx) == "missing"
    ti.index_transcripts(root, idx)
    assert ti.search("zebra gateway", idx) and ti.index_status(idx) == "ok"

    with sqlite3.connect(idx) as con:  # what the unsanitised T-1860 code left behind: no version marker
        con.execute("PRAGMA user_version = 0")
        con.execute("DELETE FROM fragments_fts")
        con.execute("DELETE FROM fragments")
        con.execute("INSERT INTO fragments(session_id, scope, date, first_line, last_line, text) "
                    "VALUES ('x', 'project:alpha-app', '2026-01-01T00:00:00Z', 0, 0, "
                    "'legacy ' || ? || ' zebra gateway')", (LEGACY_KEY,))
        con.execute("INSERT INTO fragments_fts(rowid, text) SELECT id, text FROM fragments")
    assert ti.index_status(idx) == "outdated"
    assert ti.search("zebra gateway", idx) == []  # an old-format index is never served

    stats = ti.index_transcripts(root, idx)
    assert stats["rebuilt"] == 1 and ti.index_status(idx) == "ok"
    assert all(LEGACY_KEY not in t for t in _fragment_texts(idx))
    hits = ti.search("zebra gateway", idx)
    assert hits and "legacy" not in hits[0].excerpt


def test_n9_an_expired_deadline_covers_connect_and_postprocessing(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    _write(root / "p" / "s.jsonl", [_rec("assistant", "Fresh zebra message about the gateway deployment.",
                                         "2026-05-01T00:00:00Z")])
    idx = tmp_path / "t.db"
    ti.index_transcripts(root, idx)
    real = ti.connect_ro

    def slow_connect(*args, **kwargs):
        import time
        time.sleep(0.15)  # lock/open contention that burns the whole budget before any query runs
        return real(*args, **kwargs)

    monkeypatch.setattr(ti, "connect_ro", slow_connect)
    assert ti.search("zebra gateway", idx, deadline_ms=50) == []
    assert ti.search("zebra gateway", idx, deadline_ms=5000)  # a sane budget still answers


def test_cli_search_tells_the_operator_to_rebuild_an_outdated_index(tmp_path, capsys, monkeypatch):
    from memorymaster.surfaces.cli import main as cli_main

    root = tmp_path / "projects"
    _write(root / "p" / "s.jsonl", [_rec("assistant", "Fresh zebra message about the gateway deployment.",
                                         "2026-05-01T00:00:00Z")])
    idx = tmp_path / "t.db"
    ti.index_transcripts(root, idx)
    with sqlite3.connect(idx) as con:
        con.execute("PRAGMA user_version = 0")
    monkeypatch.chdir(tmp_path)
    assert cli_main(["transcript-search", "zebra gateway", "--index", str(idx)]) == 0
    assert "memorymaster transcript-index" in capsys.readouterr().out
