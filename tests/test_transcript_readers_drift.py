"""T-2548 item 2: the raw-transcript readers against drift found on the real stores (D1-D8).

One block per drift, each driven through ``index_transcripts`` on synthetic stores in tmp dirs.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
from test_transcript_index_sources import (
    SECRET,
    _codex_line,
    _codex_meta,
    _codex_msg,
    world,
    write_codex,
)

from memorymaster.recall import transcript_index as ti

IMPORTED_FIXTURES = (world,)  # referenced, so pytest sees the fixture and ruff keeps the import


# --- D1: Codex compressed rollouts ----------------------------------------------------------------------------

def _zst(data: bytes) -> bytes:
    try:
        from compression import zstd  # Python 3.14+

        return zstd.compress(data)
    except ImportError:
        pass
    try:
        import zstandard
    except ImportError:
        pytest.skip("no zstd compressor importable (stdlib compression.zstd needs Python 3.14, else the zstandard package)")
    return zstandard.ZstdCompressor().compress(data)


def _lines(lines: list[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_codex_zst(home: Path, name: str, lines: list[str], *, frames: int = 1) -> Path:
    """A compressed rollout; ``frames`` > 1 cuts the lines into that many separately compressed frames."""
    path = home / ".codex" / "sessions" / "2026" / "11" / "02" / f"rollout-2026-11-02T10-00-00-{name}.jsonl.zst"
    path.parent.mkdir(parents=True, exist_ok=True)
    size = max(1, -(-len(lines) // frames))
    path.write_bytes(b"".join(_zst(_lines(lines[i:i + size])) for i in range(0, len(lines), size)))
    return path


def _rollout(world, question: str = "What retention window should the quartzite audit logs have?",
             answer: str = "We decided the quartzite audit logs retention window is 45 days, final.") -> list[str]:
    return [_codex_meta(world.cwd), _codex_msg("user", question), _codex_msg("assistant", answer)]


def test_a_compressed_codex_rollout_is_indexed_under_the_name_without_zst(world):
    write_codex_zst(world.home, "0198a1b2", _rollout(world))
    stats = world.index()
    assert stats["codex_files_indexed"] == 1 and stats["errors"] == 0 and stats["skipped_no_zstd"] == 0
    [hit] = world.search("quartzite audit logs retention window")
    assert hit.source == "codex" and hit.scope == "project:alpha-app" and hit.date == "2026-11-02T10:00:00Z"
    [(rel,)] = world.rows("SELECT rel FROM files")
    assert rel == "codex:2026/11/02/rollout-2026-11-02T10-00-00-0198a1b2.jsonl"
    text = world.rows("SELECT text FROM fragments")[0][0]
    assert "user: What retention" in text and "assistant: We decided" in text  # both roles
    assert world.index()["files_skipped"] == 1  # a compressed rollout is immutable: unchanged = skipped


def test_a_compressed_rollout_with_a_plain_twin_is_one_session_not_two(world):
    write_codex(world.home, "0198a1b2", _rollout(world))
    write_codex_zst(world.home, "0198a1b2", _rollout(world))
    stats = world.index()
    assert stats["codex_files_seen"] == 1 and stats["codex_files_indexed"] == 1
    assert world.rows("SELECT count(*) FROM files") == [(1,)] and world.rows("SELECT count(*) FROM fragments") == [(1,)]


def test_a_session_indexed_while_plain_survives_being_compressed_even_without_a_decompressor(world, monkeypatch):
    plain = write_codex(world.home, "0198a1b2", _rollout(world))
    world.index()
    [(sid,)] = world.rows("SELECT session_id FROM files")
    plain.unlink()
    write_codex_zst(world.home, "0198a1b2", _rollout(world))
    real = ti._zstd_backend
    monkeypatch.setattr(ti, "_zstd_backend", lambda: None)
    stats = world.index()
    assert stats["files_removed"] == 0 and stats["codex_skipped_no_zstd"] == 1 and stats["skipped_no_zstd"] == 1
    assert world.rows("SELECT session_id FROM fragments") == [(sid,)]  # not purged
    assert len(world.search("quartzite audit logs retention window")) == 1
    monkeypatch.setattr(ti, "_zstd_backend", real)
    stats = world.index()  # with a decompressor the same session is re-read whole under the same key
    assert stats["files_removed"] == 0 and stats["codex_files_indexed"] == 1
    assert world.rows("SELECT session_id FROM fragments") == [(sid,)]


def test_no_decompressor_counts_each_file_and_logs_once_and_never_fails(world, monkeypatch, caplog):
    write_codex_zst(world.home, "aaaa0001", _rollout(world))
    write_codex_zst(world.home, "aaaa0002", _rollout(world, "A question about the marzipan snapshot cadence today?"))
    write_codex(world.home, "plain001", _rollout(world, "A plain question about the heron failover plan today?"))
    monkeypatch.setattr(ti, "_zstd_backend", lambda: None)
    with caplog.at_level(logging.WARNING, logger=ti.logger.name):
        stats = world.index()
    assert stats["codex_skipped_no_zstd"] == 2 and stats["skipped_no_zstd"] == 2
    assert stats["errors"] == 0 and stats["sources_failed"] == 0 and stats["codex_files_indexed"] == 1  # the plain one works
    assert sum("zstd" in record.getMessage().lower() for record in caplog.records) == 1
    assert world.index()["skipped_no_zstd"] == 2  # still counted every run, so the job log keeps saying it


def test_the_backend_is_stdlib_then_zstandard_then_none(monkeypatch):
    ti._zstd_backend.cache_clear()
    monkeypatch.setitem(sys.modules, "compression.zstd", None)  # an import of it raises ImportError
    monkeypatch.setitem(sys.modules, "zstandard", None)
    try:
        assert ti._zstd_backend() is None
    finally:
        ti._zstd_backend.cache_clear()


def test_the_zstandard_backend_reads_a_rollout_when_the_stdlib_one_is_missing(world, monkeypatch):
    pytest.importorskip("zstandard", reason="zstandard is not installed here (it is the fallback for Python < 3.14)")
    write_codex_zst(world.home, "0198a1b2", _rollout(world))
    ti._zstd_backend.cache_clear()
    monkeypatch.setitem(sys.modules, "compression.zstd", None)
    try:
        assert world.index()["codex_files_indexed"] == 1
        assert len(world.search("quartzite audit logs retention window")) == 1
    finally:
        ti._zstd_backend.cache_clear()


def test_a_multi_frame_compressed_rollout_is_read_whole_and_streamed_line_by_line(world):
    lines = [_codex_meta(world.cwd)] + [
        _codex_msg("user", f"Question number {i} about the nimbus pipeline throughput budget?") for i in range(40)]
    write_codex_zst(world.home, "0198a1b2", lines, frames=4)
    assert world.index()["codex_files_indexed"] == 1
    text = " ".join(row[0] for row in world.rows("SELECT text FROM fragments"))
    assert "Question number 0 " in text and "Question number 39 " in text  # first and last frame


def test_a_corrupt_compressed_rollout_is_one_error_and_the_rest_still_index(world):
    bad = write_codex_zst(world.home, "bad00001", _rollout(world))
    bad.write_bytes(bad.read_bytes()[:-9] + b"\x00garbage!")
    write_codex(world.home, "good0001", _rollout(world, "A plain question about the heron failover plan today?"))
    stats = world.index()
    assert stats["codex_errors"] == 1 and stats["codex_files_indexed"] == 1 and stats["sources_failed"] == 0
    assert len(world.search("heron failover plan")) == 1


def test_a_secret_in_a_compressed_rollout_never_reaches_the_sidecar(world):
    write_codex_zst(world.home, "0198a1b2", _rollout(world, f"Remember that the quartzite deploy key is {SECRET} for later."))
    world.index()
    assert SECRET.encode() not in world.sidecar_bytes()
    assert len(world.search("quartzite deploy key")) == 1


def test_the_zstd_extra_is_declared_optional_in_pyproject():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'zstd = ["zstandard' in text
    assert "zstandard" not in text.split("[project.optional-dependencies]")[0]  # not a hard dependency



# --- D2: Codex Desktop re-stamped replays ---------------------------------------------------------------------

TURN_A = "0198aaaa-1111-4222-8333-444455556666"
TURN_B = "0198bbbb-1111-4222-8333-444455556666"
RESTAMP = "2026-11-07T12:00:00Z"  # the write time every record of a re-stamped rollout carries
STARTED_AT = 1793871045  # 2026-11-05T09:30:45Z, the real turn clock


def _turn(turn_id: str, started_at: int | None, question: str, answer: str, ts: str = RESTAMP) -> list[str]:
    started = {"type": "task_started", "turn_id": turn_id, **({"started_at": started_at} if started_at else {})}
    return [_codex_line("event_msg", started, ts), _codex_msg("user", question, ts), _codex_msg("assistant", answer, ts)]


def _dates(world) -> list[str]:
    return [row[0] for row in world.rows("SELECT date FROM fragments ORDER BY id")]


def test_a_restamped_rollout_is_dated_from_the_turn_clock(world):
    write_codex(world.home, "0198a000", [
        _codex_meta(world.cwd),
        *_turn(TURN_A, STARTED_AT, "What retention window should the quartzite audit logs have?",
               "We decided the quartzite audit logs retention window is 45 days, final.")])
    world.index()
    assert _dates(world) == ["2026-11-05T09:30:45Z"]
    [hit] = world.search("quartzite audit logs retention window")
    assert hit.date == "2026-11-05T09:30:45Z"


def test_a_grown_rollout_keeps_the_turn_date_on_resume_and_a_new_turn_gets_its_own(world):
    path = write_codex(world.home, "0198a000", [
        _codex_meta(world.cwd),
        *_turn(TURN_A, STARTED_AT, "What retention window should the quartzite audit logs have?",
               "We decided the quartzite audit logs retention window is 45 days, final.")])
    world.index()
    with path.open("a", encoding="utf-8") as fh:  # more of the same turn: no new task_started
        fh.write(_codex_msg("user", "Follow up about the okapi cache eviction policy please.", RESTAMP) + "\n")
    world.index()
    assert _dates(world) == ["2026-11-05T09:30:45Z", "2026-11-05T09:30:45Z"]
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(_turn(TURN_B, STARTED_AT + 86400, "A new question about the pumice scheduler backlog today?",
                                 "The pumice scheduler backlog drains by friday, final answer.")) + "\n")
    world.index()
    assert _dates(world)[-1] == "2026-11-06T09:30:45Z"
    assert world.rows("SELECT count(*) FROM fragments") == [(3,)]


def test_a_rollout_without_a_turn_clock_keeps_the_record_timestamp(world):
    write_codex(world.home, "0198a000", [
        _codex_meta(world.cwd), _codex_line("event_msg", {"type": "task_started", "turn_id": TURN_A}, RESTAMP),
        _codex_msg("user", "What retention window should the quartzite audit logs have?", "2026-11-04T08:00:00Z")])
    world.index()
    assert _dates(world) == ["2026-11-04T08:00:00Z"]


def _replay_pair(world) -> tuple[Path, Path]:
    question, answer = "What retention window should the quartzite audit logs have?", "We decided it is 45 days, final."
    original = write_codex(world.home, "0198a000", [_codex_meta(world.cwd), *_turn(TURN_A, STARTED_AT, question, answer)])
    replay = write_codex(world.home, "0198b000", [
        _codex_meta(world.cwd), *_turn(TURN_A, STARTED_AT, question, answer),
        *_turn(TURN_B, STARTED_AT + 60, "A new question about the pumice scheduler backlog today?",
               "The pumice scheduler backlog drains by friday, final answer.")])
    return original, replay


def _fragments_of(world, name: str) -> int:
    return world.rows(f"SELECT count(*) FROM fragments WHERE session_id IN "
                      f"(SELECT session_id FROM files WHERE rel LIKE '%{name}.jsonl')")[0][0]


def test_a_turn_already_indexed_from_another_rollout_is_not_indexed_twice_and_the_earliest_file_wins(world):
    _replay_pair(world)
    world.index()
    assert len(world.search("quartzite audit logs retention window", limit=5)) == 1  # not two sessions
    assert len(world.search("pumice scheduler backlog")) == 1  # the replay's own turn is kept
    assert _fragments_of(world, "0198a000") == 1 and _fragments_of(world, "0198b000") == 1
    assert "quartzite" in world.rows("SELECT text FROM fragments WHERE session_id IN "
                                     "(SELECT session_id FROM files WHERE rel LIKE '%0198a000.jsonl')")[0][0]


def test_lines_appended_to_a_skipped_turn_stay_skipped_on_resume(world):
    _replay_pair(world)
    copy = write_codex(world.home, "0198c000", [
        _codex_meta(world.cwd),
        _codex_line("event_msg", {"type": "task_started", "turn_id": TURN_A, "started_at": STARTED_AT}, RESTAMP),
        _codex_msg("user", "Which retention window should the quartzite audit logs have?", RESTAMP)])
    world.index()
    assert _fragments_of(world, "0198c000") == 0
    with copy.open("a", encoding="utf-8") as fh:  # still inside the copied turn: no new task_started
        fh.write(_codex_msg("assistant", "A copied answer about the quartzite audit logs retention.", RESTAMP) + "\n")
    world.index()
    assert _fragments_of(world, "0198c000") == 0
    with copy.open("a", encoding="utf-8") as fh:  # its own turn afterwards is indexed
        fh.write("\n".join(_turn(TURN_B.replace("bbbb", "cccc"), STARTED_AT + 120, "A brand new question about the heron ingest backlog?",
                                 "The heron ingest backlog is cleared every night, final.")) + "\n")
    world.index()
    assert _fragments_of(world, "0198c000") == 1 and len(world.search("heron nightly cleared")) == 1


def test_a_full_reparse_of_both_rollouts_gives_the_same_result(world):
    _replay_pair(world)
    world.index()
    before = world.rows("SELECT session_id, text FROM fragments ORDER BY session_id, id")
    with closing(sqlite3.connect(world.idx)) as con:
        con.execute("UPDATE files SET size = -1, prefix_sha = '' WHERE source = 'codex'")
        con.commit()
    assert world.index()["codex_files_indexed"] == 2
    assert world.rows("SELECT session_id, text FROM fragments ORDER BY session_id, id") == before


def test_turns_without_a_uuid_id_are_never_treated_as_copies(world):
    for name in ("0198a000", "0198b000"):  # imported turns reuse one literal id across hundreds of unrelated rollouts
        write_codex(world.home, name, [
            _codex_meta(world.cwd),
            *_turn("external-import-turn-1", None, f"A question {name} about the marzipan snapshot cadence?",
                   f"The marzipan snapshot cadence for {name} is every 7 hours, final.")])
    world.index()
    assert world.rows("SELECT count(*) FROM fragments") == [(2,)]


def test_a_copy_that_was_indexed_before_its_original_stays_known_residue(world):
    """Limit, pinned on purpose: the earliest file wins only in the order the index meets them.  A replay indexed in an
    earlier run keeps its turn when the original appears later (no per-turn fragment ids exist to retract it)."""
    question, answer = "What retention window should the quartzite audit logs have?", "We decided it is 45 days, final."
    write_codex(world.home, "0198b000", [_codex_meta(world.cwd), *_turn(TURN_A, STARTED_AT, question, answer)])
    world.index()
    write_codex(world.home, "0198a000", [_codex_meta(world.cwd), *_turn(TURN_A, STARTED_AT, question, answer)])
    world.index()
    assert _fragments_of(world, "0198b000") == 1 and _fragments_of(world, "0198a000") == 0


def test_the_state_of_an_existing_row_keeps_its_old_shape_without_turn_data(world):
    path = write_codex(world.home, "0198a000", [_codex_meta(world.cwd), _codex_msg("user", "A message about the kestrel failover plan.")])
    world.index()
    assert world.rows("SELECT state FROM files") == [("project:alpha-app",)]  # a bare scope, as before
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_codex_msg("user", "Follow up about the okapi cache eviction policy please.") + "\n")
    world.index()
    assert len(world.search("okapi cache eviction")) == 1
