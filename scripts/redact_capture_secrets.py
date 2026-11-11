#!/usr/bin/env python3
"""Redact, in place and irreversibly, secrets that sit at rest in the dream capture ledger.

WHY. Captures are sanitised when they are written (``dreaming/capture.py:_sanitize``: the literal patterns
of ``core.security.redact_text`` plus absolute Windows paths). Patterns added later (Spanish password prose,
basic-auth URLs, AWS secrets, OTP/PIN, CUIT ...) never reached the rows stored before them. Egress to a
provider is already gated (``core/provider_egress.py``); this tool cleans what sits at rest.

DECISION (operator, 2026-11-11): replace only the matching spans with markers; irreversible; captures stay
usable for extraction. No whole-capture wipe, no copy of the original is kept.

Usage (dry run is the default and opens the file read-only):
    python scripts/redact_capture_secrets.py --ledger <capture-control.db>
    python scripts/redact_capture_secrets.py --ledger <capture-control.db> --apply
Exit 0: no row failed and (``--apply``) nothing is left to redact. Exit 1: a row could not be processed or was
skipped busy (ids and a reason code on stderr, never content). A dry run exits 0 when every row could be scanned.

WHAT IS REDACTED (per message ``text``, and per string leaf of ``extraction_json`` / ``decisions_json``,
and ``last_error``)
* literal matches of ``redact_text`` and of the capture-time absolute-path rule are replaced by their
  ``[REDACTED:<name>]`` marker in place, to a fixed point (so a second run finds nothing);
* if the result still carries a credential-grade finding that only the scan variants can see (a credential
  ENCODED as base64 / hex escapes / JSON / confusable or zero-width characters: it cannot be located in the
  original bytes) the text of THAT message is replaced by ``[WITHHELD:credential_grade]`` (the marker the egress
  gate uses). Its id, role and timestamp stay. Nothing else in the capture is touched.
* private IPv4, e-mail addresses and POSIX paths that ``redact_text`` does not match are NOT redacted: the
  capture-time sanitiser never did, the sensitivity rule keeps private IPs on purpose, and the egress gate
  redacts them when a text leaves. Stored text therefore equals what today's capture would store for the same
  input (``_sanitize``, pinned by a test) EXCEPT for encoded credentials: today's capture keeps them verbatim
  and only the egress gate withholds them; this tool withholds them at rest.
* other text columns of ``dream_captures``: ``capture_key`` / ``session_hash`` / ``run_id`` / ``extraction_run_id``
  are hashes or uuids, ``scope`` is a slug and an authorisation key, ``provider`` / ``state`` / ``version`` /
  ``deferred_reason`` are enums, the rest are timestamps and numbers: none can carry captured text, none is read.
  String leaves under id/hash/enum keys of the JSON columns (``*_id``, ``*_hash``, ``action``, ``claim_type``,
  ``scope_class``, ``valid_*``, ``timestamp``) are skipped: rewriting an id would orphan its decision.

ROW CONSISTENCY, field by field (readers: ``dreaming/ledger.py``, ``dreaming/worker.py``,
``dreaming/extraction_window.py``, ``dreaming/source_review.py``, ``dreaming/history_inventory.py``,
``dreaming/backfill.py``). Only a row whose text changed is written; every other row stays byte-identical.
* ``messages_json``: the only message field written is ``text``; order, ids, roles, timestamps, extra keys stay.
* ``turn_count``: STAYS. No message is dropped, a withheld one keeps its slot.
* ``byte_count``: RECOMPUTED as ``enqueue`` / the coalescer do (utf-8 length of the stored JSON): the 1 MiB slow
  lane rule and retention pruning read it.
* ``content_hash``: RECOMPUTED as the coalescer does (sha256 of the messages, ``sort_keys``). It is the compare-
  and-swap token of the capture coalescer (``_coalesce_once``: ``WHERE ... content_hash=?``): a producer that
  read the raw text before this write and appends after it fails its CAS, re-reads the redacted row and merges
  into that, so the raw text cannot come back. Unchanged, it would let a stale merge overwrite the redaction.
  Readers of the hash: ``capture_hash`` seeds candidate ids of NOT yet extracted windows only (finished
  candidates keep their stored ids), ``history_inventory`` / backfill use it as a fingerprint of the content.
  Nothing compares it with the transcript (the transcript side is the byte cursor, not a hash).
* ``capture_key`` (UNIQUE): when it ends with the old content hash (a row that was never coalesced) the hash
  part is rewritten, so re-reading the same range today (it would sanitise to the same text) still dedupes in
  ``enqueue``. Skipped when the new key already exists. Nothing else reads it.
* ``cursor_start`` / ``cursor_end``: STAY. They are byte offsets of the TRANSCRIPT; the per-session byte cursor
  lives in ``capture_cursors`` (same file) and is not touched, so the next capture reads only new bytes.
* ``extracted_through``: it counts extraction UNITS (``extraction_window.units``: pieces of at most 16,000
  chars) and redaction changes message lengths. NULL stays NULL (historical row, never re-windowed). A complete
  cursor (>= old unit count) becomes the NEW unit count (else ``extraction_complete`` would turn false and the
  row would be extracted again). A partial cursor is mapped through the covered character offset of each
  message (floor: at worst a piece is sent again, never skipped).
* ``state``, ``attempts``, ``error_count``, ``run_id``, ``resume_eligible``, ``held_count``, ``deferred_reason``,
  ``extraction_run_id``, ``source_mtime``, ``captured_at``, ``last_activity_at``: STAY. In particular
  ``updated_at`` is not bumped: the retry/backoff and retention rules compare it, and an edit is not activity.
  ``dream_applications`` keys are ``capture id|candidate id|action``: untouched, so an applied row is not re-applied.
* ``extraction_json`` / ``decisions_json`` / ``last_error``: redacted in place (same rule); no hash covers them.

CONCURRENCY. The Beat task works with a lease (``dream_leases``: ``dream-worker`` for the dream run,
``backfill-runner`` for backfill) that is global, not per row; a row has no lock of its own. A row in a state a
live worker may be using (captured, retryable, extracted, consolidated) is skipped while such a lease is live
and reported ``skipped_busy`` (rerun later). Text is scanned OUTSIDE any transaction; each row is then one
``BEGIN IMMEDIATE`` that re-reads the guarded columns, compares them with the scanned snapshot (compare-and-swap:
anything that moved the row makes it rescan, three attempts, then ``skipped_busy``) and writes. A failure rolls
the row back whole.

AUDIT. One stdout line per redacted row: id, state, pattern names, message count, columns. The ledger has no
event/audit table for a row edit (``dream_runs`` / ``dream_hook_errors`` are not that) and none is invented:
keep the stdout. Content is never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memorymaster.core.provider_egress import WITHHELD  # noqa: E402
from memorymaster.core.security import redact_text, scan_text_for_findings  # noqa: E402
from memorymaster.dreaming.capture import _ABSOLUTE_WINDOWS_PATH  # noqa: E402
from memorymaster.dreaming.extraction_window import units  # noqa: E402
from memorymaster.stores._storage_shared import connect_ro, open_conn  # noqa: E402

_MARKER = re.compile(r"\[(?:REDACTED|WITHHELD):[A-Za-z0-9_#]+\]")
_PASSES = 3  # fixed-point bound for the literal pass
_ATTEMPTS = 3  # rescans of a row that moved under us
_BATCH = 200
_WORKER_STATES = frozenset({"captured", "retryable", "extracted", "consolidated"})
_LEASES = ("dream-worker", "backfill-runner")
# JSON keys whose string leaves are identifiers / enums / timestamps: never rewritten.
_SKIP_KEYS = frozenset({"id", "action", "claim_type", "scope_class", "valid_from", "valid_until", "timestamp"})
_GUARD = (
    "messages_json", "extraction_json", "decisions_json", "last_error", "state", "updated_at",
    "extracted_through", "content_hash", "capture_key", "run_id",
)


# --- text rules ---------------------------------------------------------------------------------------


def _literal(text: str) -> tuple[str, list[str]]:
    """The capture-time sanitiser without ``.strip()``, to a fixed point; names of what changed the text."""
    names: list[str] = []
    for _ in range(_PASSES):
        out, found = redact_text(text)
        swapped = _ABSOLUTE_WINDOWS_PATH.sub("[REDACTED:absolute_path]", out)
        if swapped == text:
            break
        names.extend([*found, "absolute_path"] if swapped != out else found)
        text = swapped
    return text, names


def _clean(text: str) -> tuple[str, list[str], bool]:
    """``(new text, pattern names, withheld)``; unchanged text comes back as is with no names."""
    new, names = _literal(text)
    # Our own markers are neutralised before scanning, as the egress gate does (a marker name can look like a token).
    encoded = scan_text_for_findings(_MARKER.sub(" ", new))
    if encoded:
        return WITHHELD, [*names, *(f"encoded:{name}" for name in encoded)], True
    return new, names, False


def _walk(value: Any, key: str | None, names: list[str]) -> Any:
    if isinstance(value, str):
        if key is not None and (key in _SKIP_KEYS or key.endswith(("_id", "_ids", "_hash"))):
            return value
        new, found, _ = _clean(value)
        if new != value:
            names.extend(found)
        return new
    if isinstance(value, list):
        return [_walk(item, key, names) for item in value]
    if isinstance(value, dict):
        return {k: _walk(item, k if isinstance(k, str) else None, names) for k, item in value.items()}
    return value


def _remap_cursor(old: list[dict[str, Any]], new: list[dict[str, Any]], cursor: int | None) -> int | None:
    """``extracted_through`` after the texts changed length (see the module docstring)."""
    if cursor is None:
        return None
    old_units, new_units = units(old), units(new)
    if cursor >= len(old_units):
        return len(new_units)
    covered: dict[int, int] = {}
    for index, _start, end in old_units[:cursor]:
        covered[index] = max(covered.get(index, 0), end)
    mapped: dict[int, int] = {}
    for index, end in covered.items():
        before, after = str(old[index].get("text", "")), str(new[index].get("text", ""))
        if before == after:
            mapped[index] = end
        elif end >= len(before):
            mapped[index] = len(after)
        else:
            mapped[index] = 0 if after == WITHHELD else len(_literal(before[:end])[0])
    count = 0
    for index, _start, end in new_units:
        if index not in mapped or end > mapped[index]:
            break
        count += 1
    return count


# --- planning (no transaction held) -----------------------------------------------------------------


@dataclass
class Plan:
    row_id: int
    state: str
    updates: dict[str, Any] = field(default_factory=dict)
    names: list[str] = field(default_factory=list)
    messages: int = 0
    withheld: int = 0
    rekey: str | None = None

    @property
    def columns(self) -> list[str]:
        return [c for c in ("messages_json", "extraction_json", "decisions_json", "last_error") if c in self.updates]


def _hash_messages(messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True).encode("utf-8")).hexdigest()


def plan_row(snap: dict[str, Any]) -> Plan | None:
    """What to write for one snapshot, or ``None`` when it is clean. May raise (the caller reports the id)."""
    plan = Plan(int(snap["id"]), str(snap["state"]))
    messages = json.loads(snap["messages_json"])
    if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
        raise ValueError("messages_json shape")
    rewritten: list[dict[str, Any]] = []
    for message in messages:
        text = message.get("text")
        if isinstance(text, str):
            new, found, withheld = _clean(text)
            if new != text:
                message = {**message, "text": new}
                plan.messages += 1
                plan.withheld += int(withheld)
                plan.names.extend(found)
        rewritten.append(message)
    if plan.messages:
        body = json.dumps(rewritten, ensure_ascii=False)
        digest = _hash_messages(rewritten)
        plan.updates.update(messages_json=body, byte_count=len(body.encode("utf-8")), content_hash=digest)
        if "extracted_through" in snap:
            cursor = _remap_cursor(messages, rewritten, snap["extracted_through"])
            if cursor != snap["extracted_through"]:
                plan.updates["extracted_through"] = cursor
        old_hash, key = str(snap.get("content_hash") or ""), str(snap.get("capture_key") or "")
        if old_hash and key.endswith(":" + old_hash) and old_hash != digest:
            plan.rekey = key[: -len(old_hash)] + digest
    for column in ("extraction_json", "decisions_json"):
        if snap.get(column):
            value = json.loads(snap[column])
            found: list[str] = []
            new_value = _walk(value, None, found)
            if new_value != value:
                plan.updates[column] = json.dumps(new_value, ensure_ascii=False)
                plan.names.extend(found)
    if snap.get("last_error"):
        new, found, _ = _clean(str(snap["last_error"]))
        if new != snap["last_error"]:
            plan.updates["last_error"] = new
            plan.names.extend(found)
    return plan if plan.updates else None


# --- ledger access ----------------------------------------------------------------------------------


def _columns(conn: sqlite3.Connection) -> list[str]:
    present = {row[1] for row in conn.execute("PRAGMA table_info(dream_captures)")}
    if not present:
        raise ValueError("not a dream ledger: no dream_captures table")
    return [c for c in _GUARD if c in present]


def _read(conn: sqlite3.Connection, columns: list[str], row_id: int) -> dict[str, Any] | None:
    rows = conn.execute(f"SELECT id, {', '.join(columns)} FROM dream_captures WHERE id=?", (row_id,)).fetchall()
    return dict(zip(["id", *columns], rows[0])) if rows else None


def _lease_live(conn: sqlite3.Connection) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    try:
        return bool(conn.execute(
            f"SELECT 1 FROM dream_leases WHERE name IN ({','.join('?' for _ in _LEASES)}) AND expires_at > ? LIMIT 1",
            (*_LEASES, now),
        ).fetchall())
    except sqlite3.OperationalError:
        return False


def _rekey(conn: sqlite3.Connection, row_id: int, key: str) -> None:
    if not conn.execute("SELECT 1 FROM dream_captures WHERE capture_key=? AND id!=?", (key, row_id)).fetchall():
        conn.execute("UPDATE dream_captures SET capture_key=? WHERE id=?", (key, row_id))


def _commit(conn: sqlite3.Connection, columns: list[str], snap: dict[str, Any], plan: Plan) -> str:
    """One row, one transaction. ``redacted`` / ``busy`` / ``moved``."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if snap["state"] in _WORKER_STATES and _lease_live(conn):
            conn.execute("ROLLBACK")
            return "busy"
        if _read(conn, columns, plan.row_id) != snap:
            conn.execute("ROLLBACK")
            return "moved"
        names = list(plan.updates)
        conn.execute(
            f"UPDATE dream_captures SET {', '.join(f'{n}=?' for n in names)} WHERE id=?",
            (*(plan.updates[n] for n in names), plan.row_id),
        )
        if plan.rekey:
            _rekey(conn, plan.row_id, plan.rekey)
        conn.execute("COMMIT")
        return "redacted"
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# --- run ----------------------------------------------------------------------------------------------


def run(ledger: Path, *, apply: bool, out: Any = None, err: Any = None) -> dict[str, Any]:
    out, err = out or sys.stdout, err or sys.stderr
    if not ledger.is_file():
        raise FileNotFoundError("ledger file not found")
    conn = open_conn(ledger, create=False) if apply else connect_ro(ledger)
    conn.isolation_level = None  # explicit transactions only
    summary: dict[str, Any] = {
        "rows": 0, "rows_with_findings": 0, "rows_redacted": 0, "messages_touched": 0, "messages_withheld": 0,
        "skipped_busy": 0, "failed": 0, "vanished": 0, "patterns": Counter(),
    }
    try:
        columns = _columns(conn)
        verb = "redacted" if apply else "would-redact"
        last = 0
        while True:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM dream_captures WHERE id>? ORDER BY id LIMIT ?", (last, _BATCH)).fetchall()]
            if not ids:
                break
            for row_id in ids:
                last = row_id
                summary["rows"] += 1
                try:
                    outcome = _process(conn, columns, row_id, apply)
                except Exception as exc:  # noqa: BLE001 - one bad row must not stop the run; ids and class only
                    summary["failed"] += 1
                    print(f"failed id={row_id} reason={type(exc).__name__}", file=err)
                    continue
                if outcome is None:
                    continue
                kind, plan = outcome
                if kind == "vanished":
                    summary["vanished"] += 1
                    continue
                summary["rows_with_findings"] += 1
                if kind in ("busy", "moved"):
                    summary["skipped_busy"] += 1
                    print(f"skipped_busy id={row_id} state={plan.state} reason="
                          f"{'lease' if kind == 'busy' else 'concurrent_change'}", file=err)
                    continue
                summary["rows_redacted"] += int(apply)
                summary["messages_touched"] += plan.messages
                summary["messages_withheld"] += plan.withheld
                names = sorted(set(plan.names))
                summary["patterns"].update(names)
                print(f"{verb} id={row_id} state={plan.state} patterns={','.join(names) or '-'} "
                      f"messages={plan.messages} withheld={plan.withheld} columns={','.join(plan.columns) or '-'}",
                      file=out)
    finally:
        conn.close()
    summary["patterns"] = dict(sorted(summary["patterns"].items()))
    return summary


def _process(conn: sqlite3.Connection, columns: list[str], row_id: int, apply: bool) -> tuple[str, Plan] | None:
    plan: Plan | None = None
    for _ in range(_ATTEMPTS):
        snap = _read(conn, columns, row_id)
        if snap is None:
            return ("vanished", Plan(row_id, "")) if plan is not None else None
        plan = plan_row(snap)
        if plan is None:
            return None
        if not apply:
            busy = snap["state"] in _WORKER_STATES and _lease_live(conn)
            return ("busy" if busy else "planned", plan)
        try:
            verdict = _commit(conn, columns, snap, plan)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                return "busy", plan
            raise
        if verdict != "moved":
            return ("redacted" if verdict == "redacted" else "busy", plan)
    return "moved", plan  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ledger", required=True, type=Path, help="path of the capture ledger SQLite file")
    parser.add_argument("--apply", action="store_true", help="write the redactions (default: dry run)")
    args = parser.parse_args(argv)
    try:
        summary = run(args.ledger, apply=args.apply)
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(f"error reason={type(exc).__name__}", file=sys.stderr)
        return 1
    print("summary " + json.dumps(
        {"mode": "apply" if args.apply else "dry-run", **summary}, sort_keys=True))
    if summary["failed"]:
        return 1
    return 1 if args.apply and summary["skipped_busy"] else 0


if __name__ == "__main__":
    sys.exit(main())
