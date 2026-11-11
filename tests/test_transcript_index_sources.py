"""T-2277 (audit D-1): the transcript index also reads Codex, Antigravity (agy) and OpenCode sessions.

Synthetic stores in tmp dirs only, in the real on-disk shapes: Codex rollouts, agy ``brain/<uuid>/.system_generated``
(dot-directory) + the ``conversations/<uuid>.db`` protobuf companion, and an OpenCode SQLite file with the real
table/column names.  HOME is a tmp dir so the DEFAULT roots resolve under it and the real home is never read.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_codex_recall_hook import installed as rendered_hooks  # fixture: templates rendered into tmp
from test_delivery_audit import hooked  # fixture: rendered hooks + ephemeral claims DB + tmp transcript index

from memorymaster.recall import transcript_fallback
from memorymaster.recall import transcript_index as ti

IMPORTED_FIXTURES = (hooked, rendered_hooks)  # referenced, so pytest sees them and ruff keeps the imports
SECRET = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
NOUNS = ["zorblax", "quartzite", "pelmeni", "varnish", "obelisk", "tundra", "marzipan", "gantry", "cobalt", "heron",
         "juniper", "kestrel", "lagoon", "mosaic", "nimbus", "ocelot", "pumice", "quiver", "rhubarb", "sextant",
         "thimble", "umbra", "vellum", "wombat", "xylem", "yarrow", "zephyr", "anvil", "bramble", "cinder"]
THINGS = ["cache invalidation strategy", "audit log retention window", "retry backoff ceiling", "queue shard count",
          "snapshot cadence", "token rotation period"]


# --- synthetic stores -----------------------------------------------------------------------------------------

def _codex_line(kind: str, payload: dict, ts: str = "2026-11-02T10:00:00Z") -> str:
    return json.dumps({"timestamp": ts, "type": kind, "payload": payload})


def _codex_msg(role: str, text: str, ts: str = "2026-11-02T10:00:00Z") -> str:
    part = "input_text" if role == "user" else "output_text"
    return _codex_line("response_item", {"type": "message", "role": role, "content": [{"type": part, "text": text}]}, ts)


def _codex_meta(cwd: str | None, **extra) -> str:
    return _codex_line("session_meta", {"id": "0198a1b2-c3d4-4e5f-8a9b-0c1d2e3f4a5b", **({"cwd": cwd} if cwd else {}), **extra})


def write_codex(home: Path, name: str, lines: list[str]) -> Path:
    path = home / ".codex" / "sessions" / "2026" / "11" / "02" / f"rollout-2026-11-02T10-00-00-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 127
        value >>= 7
        out.append(byte | (128 if value else 0))
        if not value:
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def write_agy(home: Path, uuid: str, cwd: str | None, rows: list[dict], product: str = "antigravity-cli",
              touched: list[str] | None = None) -> Path:
    base = home / ".gemini" / product
    logs = base / "brain" / uuid / ".system_generated" / "logs"  # a dot-directory: glob would skip it
    logs.mkdir(parents=True, exist_ok=True)
    transcript = logs / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    if cwd or touched:
        blobs = []
        if cwd:  # OLD shape: field 10 > 1 > 42 > 11 > 1 = workspace path (agy_source.read_executor_metadata)
            blobs.append(_field(10, _field(1, _field(42, _field(11, _field(1, cwd.encode()))))))
        # NEW shape (agy since ~2026-11-01): no workspace field; field 18 > 1 > 3 lists the files the session touched
        if touched:  # one blob repeating field 18, as the producer writes it (well over the reader's 64-value fan-out)
            blobs.append(b"".join(_field(18, _field(1, _field(3, path.encode()))) for path in touched))
        (base / "conversations").mkdir(exist_ok=True)
        with closing(sqlite3.connect(base / "conversations" / f"{uuid}.db")) as conn:
            conn.execute("CREATE TABLE executor_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            conn.executemany("INSERT INTO executor_metadata VALUES (?, ?)", list(enumerate(blobs)))
            conn.commit()
    return transcript


def agy_rows(question: str, answer: str, ts: str = "2026-11-03T10:00:00Z") -> list[dict]:
    return [
        {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": question, "created_at": ts},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": answer, "created_at": ts},
    ]


class OpenCode:
    """A real-shape OpenCode database: session / message / part tables with the producer's column names."""

    def __init__(self, home: Path) -> None:
        self.root = home / ".local" / "share" / "opencode"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "opencode.db"
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript("""
            CREATE TABLE session (id text PRIMARY KEY, project_id text NOT NULL, parent_id text, slug text NOT NULL,
                directory text NOT NULL, title text NOT NULL, version text NOT NULL, time_created integer NOT NULL,
                time_updated integer NOT NULL);
            CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL,
                time_updated integer NOT NULL, data text NOT NULL);
            CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
                time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
        """)
        self.n = 0

    def session(self, sid: str, directory: str, parent: str | None = None) -> None:
        self.conn.execute("INSERT INTO session VALUES (?, 'p', ?, 's', ?, 't', '1', 1, 1)", (sid, parent, directory))
        self.conn.commit()

    def message(self, sid: str, role: str, text: str, created: int = 1_793_000_000_000) -> None:
        self.n += 1
        mid, created = f"msg_{self.n:04d}", created + self.n
        self.conn.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                          (mid, sid, created, created, json.dumps({"role": role, "time": {"created": created}})))
        self.conn.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                          (f"prt_{self.n:04d}", mid, sid, created, created, json.dumps({"type": "text", "text": text})))
        self.conn.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                          (f"prt_{self.n:04d}b", mid, sid, created, created, json.dumps({"type": "tool", "tool": "bash"})))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class OpenCodeV2(OpenCode):
    """The current producer layout: ``session_v2`` + ``session_message`` (user: ``{text}``, assistant: ``{content: parts}``)."""

    def __init__(self, home: Path) -> None:
        self.root = home / ".local" / "share" / "opencode"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "opencode.db"
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript("""
            CREATE TABLE session_v2 (id text PRIMARY KEY, project_id text NOT NULL, workspace_id text, parent_id text,
                fork_session_id text, fork_boundary text, slug text NOT NULL, directory text NOT NULL, path text,
                title text, version text NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL);
            CREATE TABLE session_message (id text PRIMARY KEY, session_id text NOT NULL, type text NOT NULL,
                seq integer NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
        """)
        self.n = 0

    def session(self, sid: str, directory: str, parent: str | None = None) -> None:
        self.conn.execute("INSERT INTO session_v2 VALUES (?, 'p', NULL, ?, NULL, NULL, 's', ?, NULL, 't', '2', 1, 1)",
                          (sid, parent, directory))
        self.conn.commit()

    def message(self, sid: str, role: str, text: str, created: int = 1_793_000_000_000) -> None:
        self.n += 1
        data = {"text": text} if role == "user" else {"content": [{"type": "text", "text": text}, {"type": "tool"}]}
        self.conn.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (f"msg_{self.n:04d}", sid, role, self.n, created + self.n, created + self.n, json.dumps(data)))
        self.conn.commit()


@pytest.fixture()
def world(tmp_path, monkeypatch):
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    for name in ti.ROOT_ENVS.values():
        monkeypatch.delenv(name, raising=False)
    project, other = tmp_path / "alpha-app", tmp_path / "beta-app"
    project.mkdir()
    other.mkdir()

    class World:
        home = tmp_path
        idx = tmp_path / "idx" / "transcripts.db"
        cwd = str(project)
        other_cwd = str(other)

        def index(self, **kw) -> dict:
            return ti.index_transcripts(None, self.idx, **kw)

        def search(self, query: str, **kw):
            return ti.search(query, self.idx, **kw)

        def sidecar_bytes(self) -> bytes:
            return b"".join(p.read_bytes() for p in (self.idx, Path(f"{self.idx}-wal")) if p.exists())

        def rows(self, sql: str) -> list[tuple]:
            with closing(sqlite3.connect(self.idx)) as con:
                return con.execute(sql).fetchall()

    return World()


# --- Codex ------------------------------------------------------------------------------------------------------

def _codex_session(world, name: str = "0198a1b2", cwd: str | None = None, extra: list[str] | None = None) -> Path:
    return write_codex(world.home, name, [
        _codex_meta(cwd or world.cwd),
        _codex_msg("user", "What retention window should the quartzite audit logs have?"),
        _codex_msg("assistant", "We decided the quartzite audit logs retention window is 45 days, final."),
        *(extra or []),
    ])


def test_codex_rollout_is_indexed_dated_scoped_and_cited(world):
    _codex_session(world)
    stats = world.index()
    assert stats["codex_files_indexed"] == 1 and stats["files_indexed"] == 1 and stats["fragments_added"] == 1
    [hit] = world.search("quartzite audit logs retention window")
    assert hit.source == "codex" and hit.scope == "project:alpha-app" and hit.date == "2026-11-02T10:00:00Z"
    assert len(hit.session_id) == 16 and hit.first_line == 1 and hit.last_line == 2
    assert "45 days" in hit.excerpt


def test_codex_incremental_append_indexes_only_the_new_lines_in_the_same_scope(world):
    path = _codex_session(world)
    assert world.index()["files_indexed"] == 1
    again = world.index()
    assert again["files_indexed"] == 0 and again["files_skipped"] == 1 and again["codex_files_indexed"] == 0
    with path.open("a", encoding="utf-8") as fh:  # the appended lines carry no session_meta: scope comes from the stored state
        fh.write(_codex_msg("user", "Follow up about the okapi cache eviction policy please.") + "\n")
    inc = world.index()
    assert inc["files_indexed"] == 1 and inc["fragments_added"] == 1
    [hit] = world.search("okapi cache eviction policy")
    assert hit.scope == "project:alpha-app" and hit.first_line == 3
    assert len(world.search("quartzite retention window")) == 1  # the first fragment was not duplicated


def test_codex_rewritten_or_shrunk_rollout_is_reindexed_and_deleted_one_dropped(world):
    path = _codex_session(world)
    world.index()
    write_codex(world.home, "0198a1b2", [
        _codex_meta(world.cwd), _codex_msg("user", "A replacement about the pumice scheduler backlog today.")])
    stats = world.index()
    assert stats["files_indexed"] == 1
    assert world.search("quartzite retention") == [] and len(world.search("pumice scheduler backlog")) == 1
    path.unlink()
    assert world.index()["files_removed"] == 1
    assert world.search("pumice scheduler backlog") == [] and world.rows("SELECT count(*) FROM files") == [(0,)]


def test_codex_session_without_cwd_is_not_indexed(world):
    write_codex(world.home, "nocwd", [_codex_meta(None), _codex_msg("user", "A message about the kestrel failover plan.")])
    stats = world.index()
    assert stats["fragments_added"] == 0 and world.search("kestrel failover") == []


def test_codex_scope_follows_the_turn_context_workspace(world):
    write_codex(world.home, "two", [
        _codex_meta(world.cwd), _codex_msg("user", "Question about the heron ingest backlog in alpha."),
        _codex_line("turn_context", {"cwd": world.other_cwd}),
        _codex_msg("user", "Question about the heron ingest backlog in beta."),
    ])
    world.index()
    assert {h.scope for h in world.search("heron ingest backlog", limit=5)} == {"project:alpha-app", "project:beta-app"}
    assert [h.scope for h in world.search("heron ingest backlog", scope="project:beta-app")] == ["project:beta-app"]


def test_codex_noise_is_skipped(world):
    write_codex(world.home, "noise", [
        _codex_meta(world.cwd),
        _codex_msg("user", "<environment_context>\n  <cwd>/x</cwd> lemur harness environment\n</environment_context>"),
        _codex_msg("user", "# AGENTS.md instructions for the lemur harness repository and more words"),
        _codex_msg("user", "You have 2 orchestration messages. Run `orca orchestration check --run run_ab12`."),
        _codex_line("response_item", {"type": "function_call_output", "call_id": "c1", "output": "lemur harness tool output"}),
        _codex_line("response_item", {"type": "message", "role": "developer",
                                      "content": [{"type": "input_text", "text": "developer lemur harness instructions"}]}),
        _codex_msg("user", "A real question about the lemur harness timeout please."),
    ])
    world.index()
    [hit] = world.search("lemur harness")
    assert "real question" in hit.excerpt


def test_codex_headless_exec_sessions_are_skipped_unless_requested(world):
    write_codex(world.home, "exec", [
        _codex_meta(world.cwd, originator="codex_exec"), _codex_msg("user", "Answer only from the context about the lemur gateway.")])
    stats = world.index()
    assert stats["files_automated"] == 1 and world.search("lemur gateway") == []
    assert world.index(include_automated=True)["files_indexed"] == 1 and len(world.search("lemur gateway")) == 1


# --- Antigravity -----------------------------------------------------------------------------------------------

def test_agy_session_is_indexed_through_the_dot_directory_layout(world):
    write_agy(world.home, "11111111-2222-4333-8444-555555555555", world.cwd,
              agy_rows("Which cadence for the marzipan snapshots?", "We decided the marzipan snapshots run every 7 hours, final."))
    stats = world.index()
    assert stats["agy_files_indexed"] == 1 and stats["fragments_added"] == 1
    [hit] = world.search("marzipan snapshots cadence")
    assert hit.source == "agy" and hit.scope == "project:alpha-app" and (hit.first_line, hit.last_line) == (0, 1)
    assert "7 hours" in hit.excerpt and hit.date == "2026-11-03T10:00:00Z"
    again = world.index()
    assert again["files_indexed"] == 0 and again["files_skipped"] == 1


def test_agy_changed_session_is_replaced_whole_and_deleted_session_dropped(world):
    uuid = "11111111-2222-4333-8444-555555555555"
    transcript = write_agy(world.home, uuid, world.cwd, agy_rows("Which cadence for the marzipan snapshots?", "Every 7 hours, final answer."))
    world.index()
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"step_index": 2, "type": "USER_INPUT", "source": "USER_EXPLICIT",
                             "content": "And the gantry rollback budget is what?", "created_at": "2026-11-03T11:00:00Z"}) + "\n")
    os.utime(transcript, (transcript.stat().st_atime, transcript.stat().st_mtime + 5))
    assert world.index()["agy_files_indexed"] == 1
    assert len(world.search("gantry rollback budget")) == 1 and len(world.search("marzipan snapshots")) == 1
    assert world.rows("SELECT count(*) FROM fragments") == [(1,)]
    transcript.unlink()
    assert world.index()["files_removed"] == 1 and world.search("gantry rollback") == []


def test_agy_session_without_a_resolvable_workspace_is_not_indexed(world):
    write_agy(world.home, "11111111-2222-4333-8444-555555555555", None, agy_rows("Question about the umbra relay?", "The umbra relay answer is here."))
    stats = world.index()
    assert stats["fragments_added"] == 0 and stats["files_indexed"] == 0 and stats["agy_files_indexed"] == 0
    assert stats["skipped_no_workspace"] == 1 and stats["agy_skipped_no_workspace"] == 1
    assert world.index()["files_skipped"] == 1  # remembered: not re-read until it changes


def _touch(world, project: str, n: int) -> list[str]:
    """``n`` real files inside ``project`` (a dir of ``world``), returned as absolute paths."""
    base = Path(project)  # (no .git in tmp dirs: a subdirectory would resolve to its own basename scope)
    out = []
    for i in range(n):
        path = base / f"f{len(list(base.iterdir()))}_{i}.py"
        path.write_text("x", encoding="utf-8")
        out.append(str(path))
    return out


def test_agy_new_shape_workspace_is_the_majority_project_of_the_touched_files(world):
    own = str(world.home / ".gemini" / "antigravity-cli" / "brain" / "u" / "plan.md")  # agy's own files do not count
    touched = _touch(world, world.cwd, 5) + _touch(world, world.other_cwd, 2) + [own]
    write_agy(world.home, "11111111-2222-4333-8444-555555555555", None,
              agy_rows("Which cadence for the marzipan snapshots?", "We decided the marzipan snapshots run every 7 hours, final."),
              touched=touched)
    stats = world.index()
    assert stats["agy_files_indexed"] == 1 and stats["skipped_no_workspace"] == 0
    assert [h.scope for h in world.search("marzipan snapshots cadence")] == ["project:alpha-app"]


def test_agy_new_shape_with_many_touched_files_in_one_blob_is_resolved(world):
    write_agy(world.home, "11111111-2222-4333-8444-555555555555", None,
              agy_rows("Which cadence for the marzipan snapshots?", "We decided the marzipan snapshots run every 7 hours, final."),
              touched=_touch(world, world.cwd, 90))
    assert world.index()["agy_files_indexed"] == 1 and len(world.search("marzipan snapshots cadence")) == 1


@pytest.mark.parametrize("n, attributed", [(1, False), (2, False), (3, True)])
def test_agy_new_shape_needs_at_least_three_attributable_files(world, n, attributed):
    write_agy(world.home, "11111111-2222-4333-8444-555555555555", None,
              agy_rows("Which cadence for the marzipan snapshots?", "We decided the marzipan snapshots run every 7 hours, final."),
              touched=_touch(world, world.cwd, n))
    stats = world.index()
    assert (stats["agy_files_indexed"], stats.get("agy_skipped_no_workspace", 0)) == ((1, 0) if attributed else (0, 1))


def test_agy_new_shape_without_a_clear_majority_is_not_attributed(world):
    touched = _touch(world, world.cwd, 3) + _touch(world, world.other_cwd, 3)
    write_agy(world.home, "11111111-2222-4333-8444-555555555555", None,
              agy_rows("Which cadence for the marzipan snapshots?", "We decided the marzipan snapshots run every 7 hours, final."),
              touched=touched)
    stats = world.index()
    assert stats["agy_files_indexed"] == 0 and stats["agy_skipped_no_workspace"] == 1 and world.search("marzipan") == []


def test_agy_errors_and_thinking_are_not_statements(world):
    rows = agy_rows("Question about the vellum proxy please?", "The vellum proxy listens on the standard port number here and nothing else matters " * 3)
    rows.append({"step_index": 2, "type": "ERROR_MESSAGE", "source": "SYSTEM", "content": "vellum proxy boom stack trace text", "created_at": "2026-11-03T10:00:01Z"})
    write_agy(world.home, "11111111-2222-4333-8444-555555555555", world.cwd, rows)
    world.index()
    assert all("boom" not in h.excerpt for h in world.search("vellum proxy"))


# --- OpenCode --------------------------------------------------------------------------------------------------

def _opencode(world) -> OpenCode:
    oc = OpenCode(world.home)
    oc.session("ses_a", world.cwd)
    oc.message("ses_a", "user", "Which token rotation period for the cinder service?")
    oc.message("ses_a", "assistant", "We decided the cinder service token rotation period is 12 days, final.")
    return oc


def test_opencode_session_is_indexed_and_ordinals_are_the_range(world):
    oc = _opencode(world)
    stats = world.index()
    assert stats["opencode_files_indexed"] == 1 and stats["fragments_added"] == 1
    [hit] = world.search("cinder token rotation period")
    assert hit.source == "opencode" and hit.scope == "project:alpha-app" and (hit.first_line, hit.last_line) == (0, 1)
    assert "12 days" in hit.excerpt and hit.date.endswith("Z")
    assert world.index()["files_indexed"] == 0
    oc.close()


def test_opencode_new_message_reindexes_the_session_and_gone_session_is_dropped(world):
    oc = _opencode(world)
    world.index()
    oc.message("ses_a", "user", "Follow up about the ocelot dashboard latency please.")
    assert world.index()["opencode_files_indexed"] == 1
    assert len(world.search("ocelot dashboard latency")) == 1 and len(world.search("cinder token rotation")) == 1
    oc.conn.execute("DELETE FROM session WHERE id = 'ses_a'")
    oc.conn.commit()
    assert world.index()["files_removed"] == 1 and world.search("cinder token rotation") == []
    oc.close()


def test_opencode_subagent_sessions_and_sessions_without_directory_are_not_indexed(world):
    oc = OpenCode(world.home)
    oc.session("ses_child", world.cwd, parent="ses_a")
    oc.message("ses_child", "user", "Subagent chatter about the nimbus pipeline internals.")
    oc.session("ses_nodir", "")
    oc.message("ses_nodir", "user", "Message in a session without any workspace directory.")
    assert world.index()["fragments_added"] == 0
    oc.close()


def test_opencode_legacy_json_storage_is_read_when_there_is_no_database(world):
    storage = world.home / ".local" / "share" / "opencode" / "storage"
    for sub in ("session/proj", "message/ses_old", "part/msg_1", "part/msg_2"):
        (storage / sub).mkdir(parents=True)
    (storage / "session/proj/ses_old.json").write_text(json.dumps({"id": "ses_old", "directory": world.cwd}), encoding="utf-8")
    for n, (role, text) in enumerate([("user", "Legacy question about the quiver batch window?"),
                                      ("assistant", "The quiver batch window is 3 hours, final.")], 1):
        (storage / f"message/ses_old/msg_{n}.json").write_text(json.dumps(
            {"id": f"msg_{n}", "sessionID": "ses_old", "role": role, "time": {"created": 1_793_000_000_000 + n}}), encoding="utf-8")
        (storage / f"part/msg_{n}/prt_{n}.json").write_text(json.dumps(
            {"id": f"prt_{n}", "messageID": f"msg_{n}", "sessionID": "ses_old", "type": "text", "text": text}), encoding="utf-8")
    assert world.index()["opencode_files_indexed"] == 1
    assert "3 hours" in world.search("quiver batch window")[0].excerpt


# --- shared guarantees ----------------------------------------------------------------------------------------

def test_a_secret_in_any_source_never_reaches_the_sidecar_file(world):
    leak = f"the sextant deploy used key {SECRET} for the push"
    write_codex(world.home, "s", [_codex_meta(world.cwd), _codex_msg("assistant", f"Codex note: {leak}.")])
    write_agy(world.home, "11111111-2222-4333-8444-555555555555", world.cwd, agy_rows("Question about the sextant deploy?", f"Agy note: {leak}."))
    oc = OpenCode(world.home)
    oc.session("ses_s", world.cwd)
    oc.message("ses_s", "assistant", f"OpenCode note: {leak}.")
    stats = world.index()
    oc.close()
    assert stats["fragments_added"] == 3
    assert SECRET.encode() not in world.sidecar_bytes() and SECRET[3:].encode() not in world.sidecar_bytes()
    hits = world.search("sextant deploy key push", limit=10)
    assert {h.source for h in hits} == {"codex", "agy", "opencode"}
    assert all(SECRET not in h.excerpt and "[REDACTED" in h.excerpt for h in hits)


def test_no_username_or_raw_path_in_the_session_ids_or_fragments(world):
    _codex_session(world)
    world.index()
    blob = world.sidecar_bytes().decode("utf-8", "replace")
    assert str(world.home) not in blob and "0198a1b2" not in "".join(r[0] for r in world.rows("SELECT session_id FROM fragments"))


def test_scope_isolation_across_sources(world):
    _codex_session(world, cwd=world.cwd)
    write_agy(world.home, "11111111-2222-4333-8444-555555555555", world.other_cwd,
              agy_rows("Which retention window for the quartzite audit logs?", "We decided the quartzite audit logs window is 9 days, final."))
    world.index()
    assert [(h.source, h.scope) for h in world.search("quartzite audit logs window", scope="project:alpha-app")] == [("codex", "project:alpha-app")]
    assert [(h.source, h.scope) for h in world.search("quartzite audit logs window", scope="project:beta-app")] == [("agy", "project:beta-app")]


def test_a_corrupt_or_locked_source_is_counted_and_never_stops_the_others(world):
    _codex_session(world)
    write_codex(world.home, "garbage", ["{not json", "\x00\x01\x02", '["a list"]'])
    write_agy(world.home, "11111111-2222-4333-8444-555555555555", world.cwd, agy_rows("Question about the thimble queue?", "The thimble queue answer is forty two."))
    broken = write_agy(world.home, "99999999-2222-4333-8444-555555555555", world.cwd,
                       agy_rows("Question about the cobalt job?", "Answer about the cobalt job."))
    broken.write_text("this is not json\n", encoding="utf-8")  # a resolvable workspace but an unreadable transcript
    oc_root = world.home / ".local" / "share" / "opencode"
    oc_root.mkdir(parents=True)
    (oc_root / "opencode.db").write_bytes(b"not a sqlite database at all" * 40)
    stats = world.index()
    assert stats["opencode_errors"] == 1 and stats["opencode_files_indexed"] == 0
    assert stats["agy_errors"] == 0 and stats["agy_skipped_unreadable"] == 1 and stats["agy_files_indexed"] == 1
    assert stats["codex_errors"] == 1 and stats["codex_files_indexed"] == 1  # the garbage rollout is counted, not fatal
    assert len(world.search("quartzite audit logs retention")) == 1 and len(world.search("thimble queue")) == 1
    assert stats["errors"] == 2
    again = world.index()  # the unreadable agy session is remembered under its stamp, not re-read
    assert again.get("agy_skipped_unreadable", 0) == 0 and again["agy_files_seen"] == 2 and again["files_skipped"] >= 2


def test_a_locked_opencode_database_skips_the_round_and_keeps_the_rest(world):
    oc = _opencode(world)
    _codex_session(world)
    world.index()
    oc.message("ses_a", "user", "Follow up about the yarrow compaction cadence please.")
    oc.conn.execute("BEGIN EXCLUSIVE")  # the producer holds the write lock
    stats = world.index()
    oc.conn.rollback()
    assert stats["opencode_errors"] == 1 and stats["codex_errors"] == 0 and stats["files_indexed"] == 0
    assert len(world.search("cinder token rotation")) == 1 and world.search("yarrow compaction") == []  # earlier data untouched
    oc.close()


def test_a_source_can_be_switched_off_and_its_rows_are_dropped(world, monkeypatch):
    _codex_session(world)
    write_agy(world.home, "11111111-2222-4333-8444-555555555555", world.cwd, agy_rows("Question about the anvil jobs?", "The anvil jobs answer is nineteen."))
    world.index()
    monkeypatch.setenv(ti.ROOT_ENVS["codex"], "off")
    stats = world.index()
    assert stats["files_removed"] == 1 and "codex_files_seen" not in stats
    assert world.search("quartzite audit logs") == [] and len(world.search("anvil jobs")) == 1


def test_roots_come_from_their_environment_variables_and_a_missing_root_is_skipped(world, monkeypatch):
    elsewhere = world.home / "elsewhere"
    write_codex(elsewhere, "x", [_codex_meta(world.cwd), _codex_msg("user", "Message about the bramble rollout plan here.")])
    monkeypatch.setenv(ti.ROOT_ENVS["codex"], str(elsewhere / ".codex" / "sessions"))
    monkeypatch.setenv(ti.ROOT_ENVS["agy"], str(world.home / "no-such-dir"))
    stats = world.index()
    assert stats["codex_files_indexed"] == 1 and "agy_files_seen" not in stats and stats["errors"] == 0


def test_an_explicit_root_indexes_the_claude_root_alone(world, tmp_path):
    _codex_session(world)
    root = tmp_path / "projects" / "p"
    root.mkdir(parents=True)
    (root / "s.jsonl").write_text(json.dumps({"type": "user", "cwd": world.cwd, "timestamp": "2026-03-01T00:00:00Z",
                                              "message": {"role": "user", "content": [{"type": "text", "text": "A claude note about the zephyr migration order."}]}}) + "\n", encoding="utf-8")
    stats = ti.index_transcripts(tmp_path / "projects", world.idx)
    assert stats["files_indexed"] == 1 and "codex_files_seen" not in stats
    assert world.search("quartzite audit logs") == []


def test_an_index_built_before_the_source_column_is_migrated_not_rebuilt(world):
    world.idx.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(world.idx)) as con:
        con.executescript("""
            CREATE TABLE files (rel TEXT PRIMARY KEY, session_id TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                offset INTEGER NOT NULL, lines INTEGER NOT NULL, prefix_sha TEXT NOT NULL, automated INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE fragments (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, scope TEXT NOT NULL,
                date TEXT NOT NULL, first_line INTEGER NOT NULL, last_line INTEGER NOT NULL, text TEXT NOT NULL);
            CREATE VIRTUAL TABLE fragments_fts USING fts5(text, content='fragments', content_rowid='id', tokenize='porter unicode61');
            INSERT INTO fragments(session_id, scope, date, first_line, last_line, text) VALUES ('s', 'project:alpha-app', '2026-01-01T00:00:00Z', 0, 1, 'assistant: the old pangolin export note');
            INSERT INTO fragments_fts(rowid, text) VALUES (1, 'assistant: the old pangolin export note');
            PRAGMA user_version = 1;
        """)
    assert [h.source for h in world.search("pangolin export")] == ["claude"]  # served before any migration
    _codex_session(world)
    stats = world.index()
    assert stats["rebuilt"] == 0 and len(world.search("pangolin export")) == 1 and len(world.search("quartzite audit")) == 1


def test_fallback_label_names_the_cli_and_the_unit(world):
    _codex_session(world)
    write_agy(world.home, "11111111-2222-4333-8444-555555555555", world.cwd, agy_rows("Which cadence for the marzipan snapshots?", "We decided the marzipan snapshots run every 7 hours, final."))
    oc = _opencode(world)
    world.index()
    oc.close()
    labels = {h.source: transcript_fallback._label(h) for h in world.search("quartzite marzipan cinder retention snapshots rotation", limit=10)}
    assert labels["codex"].endswith("lines 1-2]") and " codex session=" in labels["codex"]
    assert labels["agy"].endswith("steps 0-1]") and labels["opencode"].endswith("messages 0-1]")


# --- the audit reproducer: 30 decisions per CLI reach the Claude hook ---------------------------------------------

N = 30


def _decision(i: int) -> tuple[str, str, str, str]:
    noun, thing = NOUNS[i % len(NOUNS)], THINGS[i % len(THINGS)]
    value = f"{i * 3 + 17} units"
    return noun, thing, value, f"We decided the {noun} {thing} is {value}, final."


def _ask(i: int) -> str:
    noun, thing, _, _ = _decision(i)
    return f"what {thing} did we pick for the {noun} service"


def test_thirty_codex_decisions_reach_the_claude_hook_and_the_control_direction_still_works(hooked, tmp_path):
    from test_hook_transcript_fallback import _rec

    claude_dir = tmp_path / ".claude" / "projects" / "p"
    claude_dir.mkdir(parents=True)
    for i in range(N):
        noun, thing, _, answer = _decision(i)
        value = f"plan-{noun[:3]}{i * 7 + 11}"
        (claude_dir / f"claude-{i}.jsonl").write_text(_rec(
            "assistant", f"We decided the {noun} {thing} is {value}, final.", "2026-11-01T10:00:05Z", hooked.cwd) + "\n", encoding="utf-8")
        thing_b = THINGS[(i + 1) % len(THINGS)]
        write_codex(tmp_path, f"0198a1b2-c3d4-4e5f-8a9b-{i:012d}", [
            _codex_meta(hooked.cwd), _codex_msg("user", f"What {thing_b} should the {noun} service have?"),
            _codex_msg("assistant", f"We decided the {noun} {thing_b} is {i * 3 + 17} units, final.")])
    stats = ti.index_transcripts()
    assert stats["claude_files_indexed"] == N and stats["codex_files_indexed"] == N and stats["errors"] == 0
    to_claude = to_codex = 0
    for i in range(N):
        noun, thing = NOUNS[i % len(NOUNS)], THINGS[i % len(THINGS)]
        thing_b = THINGS[(i + 1) % len(THINGS)]
        to_claude += f"{i * 3 + 17} units" in hooked.claude(f"what {thing_b} did we pick for the {noun} service", f"hc-{i}")
        to_codex += f"plan-{noun[:3]}{i * 7 + 11}" in hooked.codex(f"what {thing} did we choose for the {noun} service", f"hx-{i}")
    assert (to_claude, to_codex) == (N, N)


def test_thirty_agy_decisions_reach_the_claude_hook(hooked, tmp_path):
    for i in range(N):
        noun, thing, _, answer = _decision(i)
        write_agy(tmp_path, f"11111111-2222-4333-8444-{i:012d}", hooked.cwd,
                  agy_rows(f"What {thing} should the {noun} service use?", answer))
    stats = ti.index_transcripts()
    assert stats["agy_files_indexed"] == N and stats["errors"] == 0
    assert sum(_decision(i)[2] in hooked.claude(_ask(i), f"ha-{i}") for i in range(N)) == N


def test_thirty_opencode_decisions_reach_the_claude_hook(hooked, tmp_path):
    oc = OpenCode(tmp_path)
    for i in range(N):
        noun, thing, _, answer = _decision(i)
        oc.session(f"ses_{i:03d}", hooked.cwd)
        oc.message(f"ses_{i:03d}", "user", f"What {thing} should the {noun} service use?")
        oc.message(f"ses_{i:03d}", "assistant", answer)
    oc.close()
    stats = ti.index_transcripts()
    assert stats["opencode_files_indexed"] == N and stats["errors"] == 0
    assert sum(_decision(i)[2] in hooked.claude(_ask(i), f"ho-{i}") for i in range(N)) == N


# --- automated sessions, oversized sessions, v2 layout, honest stats (verifier round 1) --------------------------

def test_opencode_v2_tables_are_indexed(world):
    oc = OpenCodeV2(world.home)
    oc.session("ses_v2", world.cwd)
    oc.message("ses_v2", "user", "Which token rotation period for the cinder service?")
    oc.message("ses_v2", "assistant", "We decided the cinder service token rotation period is 12 days, final.")
    stats = world.index()
    assert stats["opencode_files_indexed"] == 1 and stats["fragments_added"] == 1
    [hit] = world.search("cinder token rotation period")
    assert hit.source == "opencode" and "12 days" in hit.excerpt
    oc.message("ses_v2", "user", "Follow up about the ocelot dashboard latency please.")
    assert world.index()["opencode_files_indexed"] == 1 and len(world.search("ocelot dashboard latency")) == 1
    oc.close()


@pytest.mark.parametrize("marker", ["provider-slot-3", "dreaming-opencode"])
def test_sessions_run_by_our_own_pipelines_are_skipped_in_every_source(world, marker):
    scratch = world.home / "scratch" / marker
    scratch.mkdir(parents=True)
    oc = OpenCode(world.home)
    oc.session("ses_gen", str(scratch))
    oc.message("ses_gen", "user", "Extraction prompt about the nimbus pipeline answer key.")
    oc.close()
    write_codex(world.home, "gen", [_codex_meta(str(scratch)),
                                    _codex_msg("user", "Extraction prompt about the nimbus pipeline answer key.")])
    write_agy(world.home, "11111111-2222-4333-8444-555555555555", str(scratch),
              agy_rows("Extraction prompt about the nimbus pipeline?", "The nimbus pipeline answer key is here."))
    claude = world.home / ".claude" / "projects" / "p"
    claude.mkdir(parents=True)
    (claude / "s.jsonl").write_text(json.dumps({
        "type": "user", "cwd": str(scratch), "timestamp": "2026-03-01T00:00:00Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "Extraction prompt about the nimbus pipeline answer key."}]},
    }) + "\n", encoding="utf-8")
    stats = world.index()
    assert stats["files_automated"] == 4 and stats["fragments_added"] == 0 and world.search("nimbus pipeline") == []
    assert stats["files_indexed"] == 0 and stats["opencode_files_indexed"] == 0
    assert world.index()["files_indexed"] == 0  # remembered
    assert world.index(include_automated=True)["fragments_added"] == 4  # the explicit opt-in still works


def test_the_generated_workspace_literals_still_match_the_dreaming_backfill():
    source = (Path(__file__).resolve().parents[1] / "memorymaster" / "dreaming" / "backfill.py").read_text(encoding="utf-8")
    assert all(f'"{marker}' in source for marker in ("provider-slot-", "dreaming-opencode"))


def test_an_oversized_opencode_session_is_recorded_not_an_error_and_not_reread(world, monkeypatch):
    monkeypatch.setattr("memorymaster.dreaming.capture._OPENCODE_MAX_RECORDS", 1)  # the reader's real size bound
    oc = _opencode(world)
    stats = world.index()
    assert stats["opencode_skipped_oversized"] == 1 and stats["skipped_oversized"] == 1
    assert stats["errors"] == 0 and stats["opencode_files_indexed"] == 0
    again = world.index()
    assert again["files_skipped"] == 1 and again["skipped_oversized"] == 0  # same change key: not re-read
    oc.message("ses_a", "user", "Follow up about the ocelot dashboard latency please.")
    assert world.index()["skipped_oversized"] == 1  # re-evaluated because the session changed
    oc.close()


def test_a_codex_session_without_a_workspace_is_counted_as_such_not_as_indexed(world):
    write_codex(world.home, "nocwd", [_codex_meta(None), _codex_msg("user", "A message about the kestrel failover plan.")])
    stats = world.index()
    assert stats["codex_files_indexed"] == 0 and stats["codex_skipped_no_workspace"] == 1 and stats["skipped_no_workspace"] == 1
