"""scripts/redact_capture_secrets.py: irreversible redaction of stored dream captures.

Every secret below is a fake value assembled from parts, so no scanner flags this file. Ledgers are tmp files
built with the product's own DreamLedger / capture helpers; nothing here touches a real ledger.
"""

from __future__ import annotations

import hashlib
import json
import runpy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from memorymaster.core.capture_control import CaptureLedger
from memorymaster.core.provider_egress import WITHHELD
from memorymaster.core.security import redact_text, scan_text_for_findings
from memorymaster.dreaming.capture import _sanitize, _scope, capture_transcript, parse_transcript_lines
from memorymaster.dreaming.extraction_window import extraction_complete, units
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.models import CaptureEnvelope, DreamMessage
from memorymaster.dreaming.worker import DreamConfig, DreamWorker
from scripts import redact_capture_secrets as rcs

H = runpy.run_path("tests/test_dreaming_worker.py")
NOW = H["NOW"]
BS = chr(92)

VAL = "Zr" + "9#" + "kT2m" + "Qw"  # Spanish prose password value (a symbol makes it credible)
URL_PW = "Pw" + "d4Fak" + "e7x"
PW = "Tr0ub4" + "dor&3" + "xQz"
ES = "la contraseña del servidor es " + VAL + " para el panel"
URL = "ver https://deploy:" + URL_PW + "@host.example.test/app ok"
HEX = "valor " + "".join(BS + "x%02x" % b for b in ("password=" + PW).encode()) + " fin"
B64 = "nota " + __import__("base64").b64encode(("password=" + PW + " extra").encode()).decode()
CLEAN = "hola mundo, nada raro por aqui"
HOME = "mira C:" + BS + "Users" + BS + "alice" + BS + "proj" + BS + "notes.txt ahora"
IP = "el host 10.1.2.3 y alice@example.test"
TEXTS = [ES, URL, HEX, B64, CLEAN, HOME, IP]
CLEAN_IDX, IP_IDX = 4, 6
RAW_FRAGMENTS = (VAL, URL_PW, PW)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(messages) -> str:
    return hashlib.sha256(json.dumps([m.to_dict() for m in messages], sort_keys=True).encode("utf-8")).hexdigest()


def _raw_row(ledger: DreamLedger, session: str, texts: list[str], *, cursor_end: int = 100) -> int:
    """A capture as an older sanitiser stored it: the texts are verbatim."""
    stamp = (NOW - timedelta(hours=1)).isoformat()
    messages = tuple(
        DreamMessage(f"m{i + 1}", "user" if i % 2 == 0 else "assistant", text, stamp) for i, text in enumerate(texts)
    )
    return ledger.enqueue(CaptureEnvelope(
        provider="codex", session_hash=session, scope="project:test", captured_at=NOW.isoformat(),
        last_activity_at=stamp, messages=messages, cursor_start=0, cursor_end=cursor_end,
        content_hash=_digest(messages),
    ), coalesce=False)


def _candidate() -> dict:
    return H["_Extractor"]().extract([], scope="project:test", capture_hash="h").candidates[0].to_dict()


def _build(tmp_path: Path) -> tuple[DreamLedger, dict[str, int]]:
    """One row per state, each carrying every kind of text."""
    ledger = DreamLedger(tmp_path / "capture-control.db")
    ids: dict[str, int] = {}
    for n, state in enumerate(("captured", "retryable", "extracted", "consolidated", "applied", "quarantined")):
        ids[state] = _raw_row(ledger, f"s-{state}", TEXTS, cursor_end=100 + n)
    ledger.mark_retryable(ids["retryable"], "run-1", "provider echoed " + URL)
    quoted = {**_candidate(), "evidence_quote": "clave: " + VAL, "text": "la contraseña del servidor es " + VAL}
    ledger.set_extraction(ids["extracted"], [quoted], "run-1")
    ledger.set_extraction(ids["consolidated"], [quoted], "run-1")
    ledger.set_decisions(ids["consolidated"], [
        {"candidate_id": quoted["candidate_id"], "action": "ignore", "rationale": "visto " + URL, "confidence": 0.5},
    ], "run-1")
    ledger.set_extraction(ids["applied"], [], "run-1")
    ledger.mark_applied(ids["applied"], "run-1")
    ledger.mark_quarantined(ids["quarantined"], "run-1", "bad")
    ids["skipped"] = _raw_row(ledger, "s-skipped", [ES], cursor_end=200)
    ledger.retire_below_min_turns(idle_days=0, now=NOW + timedelta(days=2))
    return ledger, ids


def _leaves(row: dict) -> list[str]:
    found: list[str] = [m["text"] for m in row["messages"]]
    if row.get("last_error"):
        found.append(row["last_error"])

    def walk(value) -> None:
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    walk(row.get("extraction"))
    walk(row.get("decisions"))
    return found


def _snapshot(ledger: DreamLedger, ids) -> dict[int, dict]:
    return {i: ledger.get_capture(i) for i in ids}


def _summary(captured: str) -> dict:
    line = [ln for ln in captured.splitlines() if ln.startswith("summary ")][-1]
    return json.loads(line.split(" ", 1)[1])


def test_dry_run_changes_no_byte_and_reports_counts_and_pattern_names(tmp_path, capsys):
    ledger, ids = _build(tmp_path)
    before_bytes, before_rows = _sha(ledger.db_path), _snapshot(ledger, ids.values())

    code = rcs.main(["--ledger", str(ledger.db_path)])

    out = capsys.readouterr()
    assert code == 0
    assert _sha(ledger.db_path) == before_bytes
    assert _snapshot(ledger, ids.values()) == before_rows
    summary = _summary(out.out)
    assert summary["mode"] == "dry-run" and summary["rows"] == 7 and summary["rows_redacted"] == 0
    assert summary["rows_with_findings"] == 7
    assert summary["messages_touched"] == 6 * 5 + 1  # es, url, hex, b64, home in each full row, es in the short one
    assert summary["messages_withheld"] == 6 * 2  # the two encoded credentials
    assert {"password_prose_es", "url_basic_auth_password", "home_path_windows",
            "encoded:password_assignment"} <= set(summary["patterns"])
    assert out.out.count("would-redact id=") == 7
    assert "withheld_reasons=encoded:password_assignment:2" in out.out
    assert summary["withheld_by_reason"] == {"encoded:password_assignment": 12}
    assert not any(fragment in out.out + out.err for fragment in RAW_FRAGMENTS)


def test_apply_redacts_every_state_and_only_the_matching_spans(tmp_path, capsys):
    ledger, ids = _build(tmp_path)
    before = _snapshot(ledger, ids.values())

    code = rcs.main(["--ledger", str(ledger.db_path), "--apply"])

    out = capsys.readouterr()
    assert code == 0 and _summary(out.out)["rows_redacted"] == 7
    assert not any(fragment in out.out + out.err for fragment in RAW_FRAGMENTS)
    for state, row_id in ids.items():
        old, new = before[row_id], ledger.get_capture(row_id)
        assert all(not scan_text_for_findings(text) for text in _leaves(new)), state
        assert not any(fragment in json.dumps(new, ensure_ascii=False) for fragment in RAW_FRAGMENTS), state
        # identity of the messages: ids, roles, timestamps, order
        assert [(m["message_id"], m["role"], m["timestamp"]) for m in new["messages"]] == [
            (m["message_id"], m["role"], m["timestamp"]) for m in old["messages"]]
        texts = [m["text"] for m in new["messages"]]
        assert texts[0] == ES.replace(VAL, "[REDACTED:password_prose_es]")
        if state == "skipped":
            continue
        assert texts[1] == URL.replace(URL_PW, "[REDACTED:url_basic_auth_password]")
        assert texts[2] == texts[3] == WITHHELD
        assert texts[CLEAN_IDX] == CLEAN and texts[IP_IDX] == IP  # byte-identical: not a pattern finding
        assert texts[5] == redact_text(HOME)[0] and "alice" not in texts[5]
    # row consistency
    for state, row_id in ids.items():
        old, new = before[row_id], ledger.get_capture(row_id)
        for kept in ("state", "turn_count", "updated_at", "attempts", "error_count", "cursor_start", "cursor_end",
                     "run_id", "last_activity_at", "extracted_through"):
            assert new[kept] == old[kept], (state, kept)
        body = json.dumps(new["messages"], ensure_ascii=False)
        assert new["byte_count"] == len(body.encode("utf-8")) and new["content_hash"] != old["content_hash"]
        assert new["content_hash"] == hashlib.sha256(json.dumps(new["messages"], sort_keys=True).encode()).hexdigest()
        assert new["capture_key"].endswith(":" + new["content_hash"])
    assert ledger.get_capture(ids["extracted"])["extraction"][0]["candidate_id"] == _candidate()["candidate_id"]
    assert "[REDACTED:" in ledger.get_capture(ids["extracted"])["extraction"][0]["evidence_quote"]
    assert ledger.get_capture(ids["consolidated"])["decisions"][0]["candidate_id"] == _candidate()["candidate_id"]
    assert "[REDACTED:" in ledger.get_capture(ids["retryable"])["last_error"]


def test_second_apply_is_a_noop(tmp_path, capsys):
    ledger, ids = _build(tmp_path)
    assert rcs.main(["--ledger", str(ledger.db_path), "--apply"]) == 0
    capsys.readouterr()
    after_first = _snapshot(ledger, ids.values())

    code = rcs.main(["--ledger", str(ledger.db_path), "--apply"])

    out = capsys.readouterr()
    assert code == 0 and _summary(out.out)["rows_with_findings"] == 0 and "redacted id=" not in out.out
    assert _snapshot(ledger, ids.values()) == after_first


def test_worker_extracts_from_a_redacted_captured_row(tmp_path):
    ledger = DreamLedger(tmp_path / "capture-control.db")
    row_id = H["_capture"](ledger, extra_text=" " + ES)
    assert VAL in json.dumps(ledger.get_capture(row_id))
    assert rcs.main(["--ledger", str(ledger.db_path), "--apply"]) == 0
    assert VAL not in json.dumps(ledger.get_capture(row_id))
    seen: list[str] = []

    class Spy(H["_Extractor"]):
        def extract(self, messages, *, scope, capture_hash):
            seen.extend(m["text"] for m in messages)
            return super().extract(messages, scope=scope, capture_hash=capture_hash)

    worker = DreamWorker(ledger, H["_service"](tmp_path), Spy(), H["_Consolidator"](),
                         config=DreamConfig(idle_minutes=30, max_sessions=20), now=lambda: NOW)
    summary = worker.run(apply_candidates=False)

    assert summary["extracted"] == 1 and summary["errors"] == 0
    assert any("[REDACTED:password_prose_es]" in text for text in seen)
    assert not any(VAL in text for text in seen)


def test_next_capture_of_a_redacted_session_appends_only_the_new_turn(tmp_path):
    repo = tmp_path / "my project"
    (repo / ".git").mkdir(parents=True)
    transcript = tmp_path / "session.jsonl"

    def line(role: str, text: str, stamp: str) -> str:
        return json.dumps({"type": role, "timestamp": stamp, "message": {"role": role, "content": text}}) + "\n"

    transcript.write_text(line("user", ES, "2026-07-21T10:00:00Z") + line("assistant", "anotado", "2026-07-21T10:01:00Z"),
                          encoding="utf-8")
    ledger = DreamLedger(tmp_path / "capture-control.db")
    session = "session-1"
    session_hash = hashlib.sha256(session.encode()).hexdigest()
    cursor_ledger = CaptureLedger(ledger.db_path)
    chunk = cursor_ledger.read_increment(transcript, f"dream:claude:{session_hash}")
    parsed = parse_transcript_lines(chunk.text, "claude")
    stored = (replace(parsed[0], text=ES), *parsed[1:])  # what the older sanitiser stored: the raw text
    row_id = ledger.enqueue(CaptureEnvelope(
        provider="claude", session_hash=session_hash, scope=_scope(str(repo)), captured_at=NOW.isoformat(),
        last_activity_at=stored[-1].timestamp, messages=stored, cursor_start=chunk.start_offset,
        cursor_end=chunk.end_offset, content_hash=_digest(stored)))
    cursor_ledger.commit_cursor(chunk)
    first_ids = [m["message_id"] for m in ledger.get_capture(row_id)["messages"]]
    assert VAL in json.dumps(ledger.get_capture(row_id))

    assert rcs.main(["--ledger", str(ledger.db_path), "--apply"]) == 0
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(line("user", "una pregunta nueva sobre el despliegue", "2026-07-21T10:02:00Z"))
    result = capture_transcript(transcript, provider="claude", session_id=session, cwd=str(repo), ledger=ledger)

    assert result == {"queued": 1, "capture_id": row_id, "messages": 1}
    row = ledger.get_capture(row_id)
    assert row["state"] == "captured" and row["turn_count"] == 3 and len(row["messages"]) == 3
    assert [m["message_id"] for m in row["messages"]][:2] == first_ids
    assert VAL not in json.dumps(row, ensure_ascii=False)
    with ledger._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM dream_captures").fetchone()[0] == 1


def test_applied_and_complete_rows_are_not_reextracted_or_reapplied(tmp_path):
    ledger = DreamLedger(tmp_path / "capture-control.db")
    # A message just under one extraction unit whose secret marker pushes it over: the unit count changes.
    tail = " " + ES
    long_text = "palabra " * ((15_995 - len(tail)) // 8) + "x" * ((15_995 - len(tail)) % 8) + tail
    assert len(long_text) == 15_995
    row_id = _raw_row(ledger, "s-long", [long_text, "Blue interfaces will be treated as your preference."])
    applied_id = _raw_row(ledger, "s-applied", TEXTS, cursor_end=300)
    ledger.set_extraction(applied_id, [], "run-1")
    ledger.mark_applied(applied_id, "run-1")
    raw = ledger.get_capture(row_id)["messages"]
    assert len(units(raw)) == 2
    ledger.save_extraction_window(row_id, [_candidate()], len(units(raw)), "run-1")
    applied_before = ledger.get_capture(applied_id)
    assert extraction_complete(ledger.get_capture(row_id))

    assert rcs.main(["--ledger", str(ledger.db_path), "--apply"]) == 0

    row = ledger.get_capture(row_id)
    assert len(units(row["messages"])) == 3 and row["extracted_through"] == 3 and extraction_complete(row)
    worker = DreamWorker(ledger, H["_service"](tmp_path), H["_NoCall"](), H["_Consolidator"](),
                         config=DreamConfig(), now=lambda: NOW)
    summary = worker.run(apply_candidates=False)
    assert summary["extracted"] == 0 and summary["errors"] == 0  # no extractor call (it would raise)
    applied_after = ledger.get_capture(applied_id)
    assert applied_after["state"] == "applied" and applied_after["attempts"] == applied_before["attempts"]
    assert applied_after["updated_at"] == applied_before["updated_at"]


def test_a_leased_row_is_skipped_and_picked_up_by_a_later_run(tmp_path, capsys):
    ledger, ids = _build(tmp_path)
    busy = ("captured", "retryable", "extracted", "consolidated")
    before = _snapshot(ledger, ids.values())
    assert ledger.acquire_lease("dream-worker", "someone-else", 3600)

    code = rcs.main(["--ledger", str(ledger.db_path), "--apply"])

    out = capsys.readouterr()
    assert code == 1 and _summary(out.out)["skipped_busy"] == len(busy)
    assert all(f"skipped_busy id={ids[state]} " in out.err for state in busy)
    for state in busy:
        assert ledger.get_capture(ids[state]) == before[ids[state]]  # untouched
    for state in ("applied", "quarantined", "skipped"):  # no live worker can be using these
        assert ledger.get_capture(ids[state]) != before[ids[state]]

    ledger.release_lease("dream-worker", "someone-else")
    assert rcs.main(["--ledger", str(ledger.db_path), "--apply"]) == 0
    assert all(not any(f in json.dumps(ledger.get_capture(i), ensure_ascii=False) for f in RAW_FRAGMENTS)
               for i in ids.values())


def test_a_failure_mid_row_leaves_that_row_unchanged_and_the_others_go_on(tmp_path, capsys, monkeypatch):
    ledger, ids = _build(tmp_path)
    target = ids["extracted"]
    before = _snapshot(ledger, ids.values())
    real = rcs._rekey

    def boom(conn, row_id, key):
        if row_id == target:
            raise RuntimeError("forced after the row UPDATE, inside the transaction")
        return real(conn, row_id, key)

    monkeypatch.setattr(rcs, "_rekey", boom)
    code = rcs.main(["--ledger", str(ledger.db_path), "--apply"])

    out = capsys.readouterr()
    assert code == 1 and f"failed id={target} table=dream_captures reason=RuntimeError" in out.err
    assert ledger.get_capture(target) == before[target]  # the UPDATE was rolled back whole
    assert ledger.get_capture(ids["captured"]) != before[ids["captured"]]

    monkeypatch.setattr(rcs, "_rekey", real)
    assert rcs.main(["--ledger", str(ledger.db_path), "--apply"]) == 0
    assert VAL not in json.dumps(ledger.get_capture(target), ensure_ascii=False)


def test_literal_pass_equals_todays_capture_sanitiser_and_encoded_credentials_are_withheld():
    for text in (ES, URL, HOME, CLEAN, IP):
        assert rcs._literal(text)[0] == _sanitize(text), "stored text must equal what today's capture would store"
    for text in (HEX, B64):
        assert _sanitize(text) == text  # today's capture keeps an encoded credential verbatim ...
        assert rcs._clean(text) == (WITHHELD, rcs._clean(text)[1], True)  # ... this tool withholds it at rest
    for text in TEXTS:
        once = rcs._clean(text)[0]
        assert rcs._clean(once)[0] == once  # idempotent


def test_missing_or_foreign_files_fail_with_a_reason_and_no_write(tmp_path, capsys):
    assert rcs.main(["--ledger", str(tmp_path / "absent.db")]) == 1
    other = tmp_path / "other.db"
    import sqlite3

    with sqlite3.connect(other) as conn:
        conn.execute("CREATE TABLE unrelated (x)")
    conn.close()
    assert rcs.main(["--ledger", str(other), "--apply"]) == 1
    assert "reason=" in capsys.readouterr().err


@pytest.mark.parametrize("cursor", [None])
def test_remap_cursor_keeps_a_historical_null(cursor):
    assert rcs._remap_cursor([{"text": "a"}], [{"text": "b"}], cursor) is None


def _big_ledger(tmp_path: Path, name: str) -> DreamLedger:
    ledger = DreamLedger(tmp_path / name)
    for n in range(5):  # each row: a 48 KB message with an encoded credential that becomes a short marker
        _raw_row(ledger, f"s-big-{n}", [f"fila{n} " * 8000 + HEX, CLEAN], cursor_end=100 + n)
    return ledger


def test_residue_check_bites_with_secure_delete_off_and_is_clean_with_it_on(tmp_path):
    off = _big_ledger(tmp_path, "off.db")
    summary = rcs.run(off.db_path, apply=True, verify_residue=True, secure_delete=False)
    assert summary["rows_redacted"] == 5 and summary["residue"] > 0  # old pages survive a TRUNCATE checkpoint

    on = _big_ledger(tmp_path, "on.db")
    summary = rcs.run(on.db_path, apply=True, verify_residue=True)
    assert summary["residue"] == 0 and summary["checkpoint_incomplete"] is False
    assert rcs.main(["--ledger", str(_big_ledger(tmp_path, "cli.db").db_path), "--apply", "--verify-residue"]) == 0


def test_remap_cursor_goes_back_rather_than_mark_unsent_text_as_sent():
    tail = " /home/alice/My Documents/Some Folder/file.txt mas texto sin enviar" + " z" * 30
    checked = 0
    for filler in range(15_860, 15_990):  # the old unit boundary (last space) falls inside the spaced match
        text = ("palabra " * 3000)[:filler] + tail
        new_text = rcs._literal(text)[0]
        old, new = [{"text": text}], [{"text": new_text}]
        if len(units(old)) < 2 or new_text == text:
            continue
        cursor = rcs._remap_cursor(old, new, 1)
        covered = units(new)[cursor - 1][2] if cursor else 0
        assert "sin enviar" in new_text[covered:], filler  # a piece may be sent again, never skipped
        checked += 1
    assert checked > 50


def test_free_text_under_skipped_looking_keys_dict_keys_and_cut_quotes_are_scanned(tmp_path):
    ledger = DreamLedger(tmp_path / "capture-control.db")
    row_id = _raw_row(ledger, "s-keys", [ES, CLEAN])
    candidate = {
        **_candidate(), "claim_type": "nota " + ES, "valid_from": "2026-01-02T03:04:05Z",
        "valid_until": "vence " + ES, "evidence_quote": "el valor era " + VAL[:7],
        "meta": {ES: "valor"},
    }
    ledger.set_extraction(row_id, [candidate], "run-1")

    assert rcs.main(["--ledger", str(ledger.db_path), "--apply"]) == 0

    new = ledger.get_capture(row_id)["extraction"][0]
    marker = "[REDACTED:password_prose_es]"
    assert new["claim_type"] == "nota " + ES.replace(VAL, marker)
    assert new["valid_until"] == "vence " + ES.replace(VAL, marker)
    assert new["valid_from"] == "2026-01-02T03:04:05Z" and new["candidate_id"] == candidate["candidate_id"]
    assert new["meta"] == {ES.replace(VAL, marker): "valor"}
    assert new["evidence_quote"] == "el valor era [REDACTED:partial_secret]"
    assert VAL[:7] not in json.dumps(ledger.get_capture(row_id), ensure_ascii=False)


def test_a_producer_appending_between_plan_and_commit_is_not_lost(tmp_path, monkeypatch):
    ledger = DreamLedger(tmp_path / "capture-control.db")
    row_id = _raw_row(ledger, "s-race", TEXTS, cursor_end=100)
    real, fired = rcs.plan_row, []
    stamp = (NOW - timedelta(minutes=30)).isoformat()

    def racing(snap, **kwargs):
        plan = real(snap, **kwargs)
        if not fired:  # the producer's append lands after the scan, before the write
            fired.append(1)
            late = DreamMessage("late-1", "user", "turno nuevo sin secretos", stamp)
            ledger.enqueue(CaptureEnvelope(
                provider="codex", session_hash="s-race", scope="project:test", captured_at=NOW.isoformat(),
                last_activity_at=stamp, messages=(late,), cursor_start=100, cursor_end=150,
                content_hash=_digest((late,))))
        return plan

    monkeypatch.setattr(rcs, "plan_row", racing)
    assert rcs.main(["--ledger", str(ledger.db_path), "--apply"]) == 0

    row = ledger.get_capture(row_id)
    assert fired and row["turn_count"] == len(TEXTS) + 1 and row["messages"][-1]["message_id"] == "late-1"
    assert row["messages"][-1]["text"] == "turno nuevo sin secretos"
    assert not any(fragment in json.dumps(row, ensure_ascii=False) for fragment in RAW_FRAGMENTS)
    with ledger._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM dream_captures").fetchone()[0] == 1


def test_auxiliary_tables_that_can_carry_captured_text_are_covered(tmp_path, capsys):
    ledger = DreamLedger(tmp_path / "capture-control.db")
    run_id = ledger.start_run(True, "m", "m", now=NOW)
    ledger.finish_run(run_id, "ok", {"note": URL, "count": 3}, now=NOW)
    meta = {"selection": {"windows": [{"identity": "abc123", "messages": [
        {"message_id": "m1", "role": "user", "text": ES, "timestamp": "2026-01-01T00:00:00Z"}]}]}}
    with ledger._connect() as conn:
        conn.execute(
            "INSERT INTO backfill_inventory(campaign_id,stream_id,cohort_id,provider,parent_session_id,"
            "source_locator,frozen_end,source_digest,status,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("c", "st", "co", "codex", "p", "loc", 1, "dg", "done", json.dumps(meta)))
    assert ledger.acquire_lease("backfill-runner", "other", 3600)

    code = rcs.main(["--ledger", str(ledger.db_path), "--apply"])

    out = capsys.readouterr()
    assert code == 1 and "skipped_busy id=1 table=backfill_inventory" in out.err  # the runner rewrites it from memory
    with ledger._connect() as conn:
        assert URL_PW not in conn.execute("SELECT summary_json FROM dream_runs").fetchone()[0]
        assert VAL in conn.execute("SELECT metadata_json FROM backfill_inventory").fetchone()[0]
    ledger.release_lease("backfill-runner", "other")
    assert rcs.main(["--ledger", str(ledger.db_path), "--apply"]) == 0
    with ledger._connect() as conn:
        stored = json.loads(conn.execute("SELECT metadata_json FROM backfill_inventory").fetchone()[0])
    window = stored["selection"]["windows"][0]
    assert window["identity"] == "abc123" and VAL not in json.dumps(stored)
    assert window["messages"][0]["text"] == ES.replace(VAL, "[REDACTED:password_prose_es]")
