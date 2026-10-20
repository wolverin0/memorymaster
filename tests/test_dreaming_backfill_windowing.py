from memorymaster.dreaming.backfill_selection import (
    MAX_RECORD_BYTES, SourceTurn, complete_records, frozen_identity, order_inventory,
    select_windows, verify_frozen,
)
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.models import CaptureEnvelope, DreamMessage


def test_windows_share_parent_allowance_and_preserve_ranges():
    turns = (SourceTurn(DreamMessage(f"m{i}", "user", ("correction decided " if i == 50 else "authored ") + "x" * 2500,
                                          "2026-10-01T00:00:00+00:00"), i * 100, (i + 1) * 100,
                        "parent" if i < 50 else "child") for i in range(100))
    result = select_windows(turns, scope="project:test")
    assert 1 <= len(result["windows"]) <= 4
    assert all(sum(len(m["text"]) for m in w["messages"]) <= 20_000 for w in result["windows"])
    assert result["sampled_out_chars"] > 0
    assert result["eligible_chars"] == result["selected_chars"] + result["sampled_out_chars"]
    assert {r["stream"] for w in result["windows"] for r in w["ranges"]} == {"parent", "child"}


def test_bounded_record_read_and_frozen_append(tmp_path):
    path = tmp_path / "stream.jsonl"
    path.write_bytes(b"{}\n" + b"x" * (MAX_RECORD_BYTES + 80) + b"\npartial")
    end, digest = frozen_identity(path)
    records = list(complete_records(path, frozen_end=end))
    assert records[0][2] == b"{}\n" and records[1][2] is None
    assert end < path.stat().st_size
    with path.open("ab") as handle:
        handle.write(b" append\n")
    assert verify_frozen(path, end, digest)
    path.write_bytes(b"changed\n")
    assert not verify_frozen(path, end, digest)


def test_priority_internal_time_and_fair_rounds():
    rows = [dict(stream_id=name, scope=scope, internal_timestamp=stamp) for name, scope, stamp in [
        ("a1", "project:a", "2026-01"), ("a2", "project:a", "2026-10"),
        ("b", "project:b", None), ("m", "project:memorymaster", "2025-01")]]
    assert [r["stream_id"] for r in order_inventory(rows)] == ["m", "a2", "b", "a1"]


def test_noncoalescing_windows_and_schema_versions_unchanged(tmp_path):
    ledger = DreamLedger(tmp_path / "fixture.db")
    first = CaptureEnvelope("codex", "parent", "project:test", "2026-01-01", "2026-01-01",
                            (DreamMessage("m1", "user", "first", "2026-01-01"),), 0, 1, "one")
    second = CaptureEnvelope("codex", "parent", "project:test", "2026-01-01", "2026-01-01",
                             (DreamMessage("m2", "user", "second", "2026-01-01"),), 1, 2, "two")
    assert ledger.enqueue(first, coalesce=False) != ledger.enqueue(second, coalesce=False)
    with ledger._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM backfill_inventory").fetchone()[0] == 0


