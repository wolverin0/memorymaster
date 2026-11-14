"""T-2548 item 2: the raw-transcript readers against drift found on the real stores (D1-D8).

One block per drift, each driven through ``index_transcripts`` on synthetic stores in tmp dirs.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from test_transcript_index_sources import (
    SECRET,
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

