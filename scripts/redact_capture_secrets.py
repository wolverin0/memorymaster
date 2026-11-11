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
    python scripts/redact_capture_secrets.py --ledger <capture-control.db> --apply --verify-residue
Exit 0: no row failed and (``--apply``) nothing is left: no busy row, checkpoint complete, no residue. Exit 1
otherwise (ids and a reason code on stderr, never content): rerun. A dry run exits 0 when every row could be scanned.

WHAT IS REDACTED (per message ``text``, and per string leaf AND dict key of the JSON columns, and ``last_error``)
* literal matches of ``redact_text`` and of the capture-time absolute-path rule are replaced by their
  ``[REDACTED:<name>]`` marker in place, to a fixed point (so a second run finds nothing);
* if the result still carries a credential-grade finding that only the scan variants can see (a credential
  ENCODED as base64 / hex escapes / JSON / confusable or zero-width characters: it cannot be located in the
  original bytes) the text of THAT message is replaced by ``[WITHHELD:credential_grade]`` (the marker the egress
  gate uses). Its id, role and timestamp stay. Nothing else in the capture is touched. The dry run prints, per
  row and in the summary, ``withheld_reasons`` (``encoded:<pattern>`` counts) so false positives (a JSON-quoted
  ``"max_tokens": 100000``) can be judged BEFORE applying. What is withheld is the scan's decision, unchanged.
* a string leaf of ``extraction_json`` / ``decisions_json`` that ends or starts with a fragment (>= 6 chars) of a
  secret found in the same capture's messages (an ``evidence_quote`` cut mid-secret) has the fragment replaced by
  ``[REDACTED:partial_secret]``.
* private IPv4, e-mail addresses and POSIX paths that ``redact_text`` does not match are NOT redacted: the
  capture-time sanitiser never did, the sensitivity rule keeps private IPs on purpose, and the egress gate
  redacts them when a text leaves. Stored text therefore equals what today's capture would store for the same
  input (``_sanitize``, pinned by a test) EXCEPT for encoded credentials: today's capture keeps them verbatim
  and only the egress gate withholds them; this tool withholds them at rest.
* a JSON value is skipped only when its key is an id / hash / enum / date AND the value has that shape
  (``*_id``, ``*_hash``, ``id``: ``[A-Za-z0-9_.:#-]{1,128}``; ``action``, ``claim_type``, ``scope_class``:
  lowercase word; ``valid_from``, ``valid_until``, ``timestamp``: ISO date). ``claim_type`` / ``valid_*`` are free
  model text: anything else under those keys is scanned.
* other text columns of ``dream_captures``: ``capture_key`` / ``session_hash`` / ``run_id`` / ``extraction_run_id``
  are hashes or uuids, ``scope`` is a slug and an authorisation key, ``provider`` / ``state`` / ``version`` /
  ``deferred_reason`` are enums, the rest are timestamps and numbers: none can carry captured text, none is read.
* other tables. ``backfill_inventory.metadata_json`` CAN carry captured text (``selection.windows[].messages``,
  discarded/sampled-out audit messages: ``dreaming/backfill.py``): covered, same rule, skipped while the
  ``backfill-runner`` lease is live because the runner rewrites the whole value from memory. ``dream_runs.summary_json``
  holds a worker summary (counts, reasons) or the backfill status file; it is covered by the same rule for safety.
  Not covered, by design: ``dream_applications`` (ids and enums), ``dream_provider_usage`` (numbers),
  ``dream_hook_errors`` (codes), ``dream_scan_state`` (paths of transcript files and message ids, no content).

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
  message; when a CHANGED message straddles the cursor the mapping of the cut is uncertain (a match cut in two
  is not redacted in the prefix) so the cursor goes back one more unit: a piece is sent again, never skipped.
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

RESIDUE AT REST. SQLite keeps the old bytes of an updated value in free pages and in the WAL. Windows Python's
SQLite defaults ``secure_delete`` OFF (Linux ON), so this tool sets ``PRAGMA secure_delete=ON`` on its write
connection and ends an ``--apply`` with ``wal_checkpoint(TRUNCATE)``; a checkpoint that a busy reader blocks is
reported ``checkpoint_incomplete`` (exit 1: rerun). ``--verify-residue`` then reads the raw bytes of the .db and
-wal files and counts the ORIGINAL matched strings of this run (>= 6 chars, JSON-escaped and plain forms; for a
withheld message 48-char probes spread over its text) that are still there; they are kept in memory only, never
printed or written; any residue is exit 1. LIMIT: free pages left by rows deleted BEFORE this run (retention
pruning) still hold their old content; only an offline ``VACUUM`` removes them and this tool does not do it.

AUDIT. One stdout line per redacted row: id, table, state, pattern names, message count, withheld reasons,
columns. The ledger has no event/audit table for a row edit (``dream_runs`` / ``dream_hook_errors`` are not that)
and none is invented: keep the stdout. Content is never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
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
_PARTIAL = "[REDACTED:partial_secret]"
_PASSES = 3  # fixed-point bound for the literal pass
_ATTEMPTS = 3  # rescans of a row that moved under us
_BATCH = 200
_MIN_NEEDLE = 6
_PROBE = 48
_MAX_PROBES = 16
_WORKER_STATES = frozenset({"captured", "retryable", "extracted", "consolidated"})
_ID_SHAPE = re.compile(r"[A-Za-z0-9_.:#-]{1,128}")
_ENUM_SHAPE = re.compile(r"[a-z][a-z_]{0,39}")
_DATE_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+Zz-]{1,40})?")
_ENUM_KEYS = frozenset({"action", "claim_type", "scope_class"})
_DATE_KEYS = frozenset({"valid_from", "valid_until", "timestamp"})
_GUARD = (
    "messages_json", "extraction_json", "decisions_json", "last_error", "state", "updated_at",
    "extracted_through", "content_hash", "capture_key", "run_id",
)
# table -> (lease names that make a row busy, state-dependent?, guarded columns, JSON column)
_AUX = {
    "backfill_inventory": (("backfill-runner",), ("metadata_json", "status", "read_offset"), "metadata_json"),
    "dream_runs": ((), ("summary_json", "status"), "summary_json"),
}


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


def _spans(orig: str, new: str) -> list[str] | None:
    """The original strings that became markers (greedy alignment of ``new`` against ``orig``), or ``None``."""
    parts = _MARKER.split(new)
    if not orig.startswith(parts[0]):
        return None
    pos, found = len(parts[0]), []
    for index in range(1, len(parts)):
        seg = parts[index]
        if index == len(parts) - 1:
            end = len(orig) - len(seg) if orig.endswith(seg) and len(orig) - len(seg) >= pos else -1
        else:
            end = orig.find(seg, pos)
        if end < 0:
            return None
        found.append(orig[pos:end])
        pos = end + len(seg)
    return found


def _probes(text: str) -> list[str]:
    if len(text) <= _PROBE:
        return [text]
    step = max(1, (len(text) - _PROBE) // (_MAX_PROBES - 1))
    return list({text[p : p + _PROBE] for p in range(0, len(text) - _PROBE + 1, step)})


def _secrets_of(orig: str, new: str, withheld: bool) -> list[str]:
    """Strings of ``orig`` that must be gone from the file after the rewrite (never printed)."""
    if withheld:
        return _probes(orig)
    spans = _spans(orig, new)
    if spans is None:
        return _probes(orig)
    return [s for s in spans if len(s) >= _MIN_NEEDLE and not _MARKER.fullmatch(s)]


@dataclass
class _Ctx:
    """Findings of one row, accumulated while its texts are rewritten."""

    secrets: list[str] = field(default_factory=list)  # matched strings of the messages (fragment rule)
    names: list[str] = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)  # encoded:<pattern> per withheld text
    needles: set[str] = field(default_factory=set)
    collect: bool = False


def _fragments(text: str, secrets: list[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, _PARTIAL)
        for k in range(min(len(secret) - 1, len(text)), _MIN_NEEDLE - 1, -1):
            if text.endswith(secret[:k]):
                text = text[:-k] + _PARTIAL
                break
        for k in range(min(len(secret) - 1, len(text)), _MIN_NEEDLE - 1, -1):
            if text.startswith(secret[-k:]):
                text = _PARTIAL + text[k:]
                break
    return text


def _leaf(text: str, ctx: _Ctx, *, fragments: bool) -> str:
    new, found, withheld = _clean(text)
    if fragments and ctx.secrets and not withheld:
        fixed = _fragments(new, ctx.secrets)
        if fixed != new:
            new, found = fixed, [*found, "partial_secret"]
    if new != text:
        ctx.names.extend(found)
        ctx.reasons.update({n for n in found if n.startswith("encoded:")})
        if ctx.collect:
            ctx.needles.update(_secrets_of(text, new, withheld))
    return new


def _plain(key: str | None, value: str) -> bool:
    """Whether ``value`` is an id / hash / enum / date by key AND by shape (then it is not text)."""
    if key is None:
        return False
    if key in _ENUM_KEYS:
        return bool(_ENUM_SHAPE.fullmatch(value))
    if key in _DATE_KEYS:
        return bool(_DATE_SHAPE.fullmatch(value))
    if key == "id" or key.endswith(("_id", "_ids", "_hash")):
        return bool(_ID_SHAPE.fullmatch(value))
    return False


def _walk(value: Any, key: str | None, ctx: _Ctx) -> Any:
    if isinstance(value, str):
        return value if _plain(key, value) else _leaf(value, ctx, fragments=True)
    if isinstance(value, list):
        return [_walk(item, key, ctx) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, item in value.items():
            new_key = _leaf(k, ctx, fragments=False) if isinstance(k, str) else k
            while new_key in out:  # two keys redacted to the same marker keep both values
                new_key = f"{new_key}#"
            out[new_key] = _walk(item, k if isinstance(k, str) else None, ctx)
        return out
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
    uncertain = False
    for index, end in covered.items():
        before, after = str(old[index].get("text", "")), str(new[index].get("text", ""))
        if before == after:
            mapped[index] = end
        elif end >= len(before):
            mapped[index] = len(after)
        else:
            uncertain = True  # a cut inside a changed text: a match split by it is not redacted in the prefix
            mapped[index] = 0 if after == WITHHELD else len(_literal(before[:end])[0])
    count = 0
    for index, _start, end in new_units:
        if index not in mapped or end > mapped[index]:
            break
        count += 1
    return max(0, count - 1) if uncertain else count


# --- planning (no transaction held) -----------------------------------------------------------------


@dataclass
class Plan:
    row_id: int
    table: str
    state: str
    updates: dict[str, Any] = field(default_factory=dict)
    names: list[str] = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)
    needles: set[str] = field(default_factory=set)
    messages: int = 0
    withheld: int = 0
    rekey: str | None = None

    @property
    def columns(self) -> list[str]:
        return [c for c in self.updates if c.endswith("_json") or c == "last_error"]


def _hash_messages(messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True).encode("utf-8")).hexdigest()


def _finish(plan: Plan, ctx: _Ctx) -> Plan | None:
    plan.names.extend(ctx.names)
    plan.reasons.update(ctx.reasons)
    plan.needles |= ctx.needles
    return plan if plan.updates else None


def plan_row(snap: dict[str, Any], *, collect: bool = False) -> Plan | None:
    """What to write for one ``dream_captures`` snapshot, or ``None`` when clean. May raise (the caller reports the id)."""
    plan = Plan(int(snap["id"]), "dream_captures", str(snap["state"]))
    ctx = _Ctx(collect=collect)
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
                ctx.names.extend(found)
                ctx.reasons.update({n for n in found if n.startswith("encoded:")})
                secrets = _secrets_of(text, new, withheld)
                ctx.secrets.extend(s for s in secrets if not withheld)
                if collect:
                    ctx.needles.update(secrets)
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
            new_value = _walk(value, None, ctx)
            if new_value != value:
                plan.updates[column] = json.dumps(new_value, ensure_ascii=False)
    if snap.get("last_error"):
        new = _leaf(str(snap["last_error"]), ctx, fragments=True)
        if new != snap["last_error"]:
            plan.updates["last_error"] = new
    return _finish(plan, ctx)


def plan_json_row(table: str, column: str, snap: dict[str, Any], *, collect: bool = False) -> Plan | None:
    """The same rule over the one JSON column of an auxiliary table row."""
    if not snap.get(column):
        return None
    ctx = _Ctx(collect=collect)
    value = json.loads(snap[column])
    new_value = _walk(value, None, ctx)
    plan = Plan(int(snap["id"]), table, str(snap.get("status") or ""))
    if new_value != value:
        plan.updates[column] = json.dumps(new_value, ensure_ascii=False)
    return _finish(plan, ctx)


# --- ledger access ----------------------------------------------------------------------------------


def _tables(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Table -> guarded columns present; a ledger without ``dream_captures`` is not ours."""
    found: dict[str, list[str]] = {}
    for table, wanted in (("dream_captures", _GUARD), *((t, spec[1]) for t, spec in _AUX.items())):
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if present:
            found[table] = [c for c in wanted if c in present]
    if "dream_captures" not in found:
        raise ValueError("not a dream ledger: no dream_captures table")
    return found


def _read(conn: sqlite3.Connection, table: str, columns: list[str], row_id: int) -> dict[str, Any] | None:
    rows = conn.execute(f"SELECT rowid, {', '.join(columns)} FROM {table} WHERE rowid=?", (row_id,)).fetchall()
    return dict(zip(["id", *columns], rows[0])) if rows else None


def _lease_live(conn: sqlite3.Connection, names: tuple[str, ...]) -> bool:
    if not names:
        return False
    now = datetime.now(timezone.utc).isoformat()
    try:
        return bool(conn.execute(
            f"SELECT 1 FROM dream_leases WHERE name IN ({','.join('?' for _ in names)}) AND expires_at > ? LIMIT 1",
            (*names, now),
        ).fetchall())
    except sqlite3.OperationalError:
        return False


def _is_busy(conn: sqlite3.Connection, table: str, snap: dict[str, Any]) -> bool:
    if table == "dream_captures":
        return snap["state"] in _WORKER_STATES and _lease_live(conn, ("dream-worker", "backfill-runner"))
    return _lease_live(conn, _AUX[table][0])


def _rekey(conn: sqlite3.Connection, row_id: int, key: str) -> None:
    if not conn.execute("SELECT 1 FROM dream_captures WHERE capture_key=? AND id!=?", (key, row_id)).fetchall():
        conn.execute("UPDATE dream_captures SET capture_key=? WHERE id=?", (key, row_id))


def _commit(conn: sqlite3.Connection, columns: list[str], snap: dict[str, Any], plan: Plan) -> str:
    """One row, one transaction. ``redacted`` / ``busy`` / ``moved``."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _is_busy(conn, plan.table, snap):
            conn.execute("ROLLBACK")
            return "busy"
        if _read(conn, plan.table, columns, plan.row_id) != snap:
            conn.execute("ROLLBACK")
            return "moved"
        names = list(plan.updates)
        conn.execute(
            f"UPDATE {plan.table} SET {', '.join(f'{n}=?' for n in names)} WHERE rowid=?",
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


def _plan(table: str, snap: dict[str, Any], collect: bool) -> Plan | None:
    if table == "dream_captures":
        return plan_row(snap, collect=collect)
    return plan_json_row(table, _AUX[table][2], snap, collect=collect)


def _process(
    conn: sqlite3.Connection, table: str, columns: list[str], row_id: int, apply: bool, collect: bool,
) -> tuple[str, Plan] | None:
    plan: Plan | None = None
    for _ in range(_ATTEMPTS):
        snap = _read(conn, table, columns, row_id)
        if snap is None:
            return ("vanished", plan) if plan is not None else None
        plan = _plan(table, snap, collect)
        if plan is None:
            return None
        if not apply:
            return ("busy" if _is_busy(conn, table, snap) else "planned", plan)
        try:
            verdict = _commit(conn, columns, snap, plan)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                return "busy", plan
            raise
        if verdict != "moved":
            return verdict, plan
    return "moved", plan  # type: ignore[return-value]


def _residue(ledger: Path, needles: set[str]) -> int:
    """How many of the original strings are still in the raw bytes of the .db / -wal files."""
    still: set[str] = set()
    for path in (ledger, ledger.with_name(ledger.name + "-wal")):
        if not path.is_file() or path.stat().st_size == 0:
            continue
        with path.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            for needle in needles - still:
                for form in (needle, json.dumps(needle, ensure_ascii=False)[1:-1]):  # plain and JSON-escaped
                    if data.find(form.encode("utf-8", errors="replace")) >= 0:
                        still.add(needle)
                        break
    return len(still)


def run(
    ledger: Path, *, apply: bool, verify_residue: bool = False, secure_delete: bool = True,
    out: Any = None, err: Any = None,
) -> dict[str, Any]:
    out, err = out or sys.stdout, err or sys.stderr
    if not ledger.is_file():
        raise FileNotFoundError("ledger file not found")
    conn = open_conn(ledger, create=False) if apply else connect_ro(ledger)
    conn.isolation_level = None  # explicit transactions only
    if apply:
        conn.execute(f"PRAGMA secure_delete={'ON' if secure_delete else 'OFF'}")
    summary: dict[str, Any] = {
        "rows": 0, "rows_with_findings": 0, "rows_redacted": 0, "messages_touched": 0, "messages_withheld": 0,
        "skipped_busy": 0, "failed": 0, "vanished": 0, "patterns": Counter(), "withheld_by_reason": Counter(),
        "by_table": Counter(), "checkpoint_incomplete": False, "residue": None,
    }
    needles: set[str] = set()
    collect = apply and verify_residue
    try:
        tables = _tables(conn)
        verb = "redacted" if apply else "would-redact"
        for table, columns in tables.items():
            last = 0
            while True:
                ids = [r[0] for r in conn.execute(
                    f"SELECT rowid FROM {table} WHERE rowid>? ORDER BY rowid LIMIT ?", (last, _BATCH)).fetchall()]
                if not ids:
                    break
                for row_id in ids:
                    last = row_id
                    summary["rows"] += 1
                    try:
                        outcome = _process(conn, table, columns, row_id, apply, collect)
                    except Exception as exc:  # noqa: BLE001 - one bad row must not stop the run; ids and class only
                        summary["failed"] += 1
                        print(f"failed id={row_id} table={table} reason={type(exc).__name__}", file=err)
                        continue
                    if outcome is None:
                        continue
                    kind, plan = outcome
                    if kind == "vanished":
                        summary["vanished"] += 1
                        continue
                    summary["rows_with_findings"] += 1
                    summary["by_table"][table] += 1
                    if kind in ("busy", "moved"):
                        summary["skipped_busy"] += 1
                        print(f"skipped_busy id={row_id} table={table} state={plan.state} reason="
                              f"{'lease' if kind == 'busy' else 'concurrent_change'}", file=err)
                        continue
                    summary["rows_redacted"] += int(apply)
                    summary["messages_touched"] += plan.messages
                    summary["messages_withheld"] += plan.withheld
                    summary["withheld_by_reason"].update(plan.reasons)
                    needles |= plan.needles
                    names = sorted(set(plan.names))
                    summary["patterns"].update(names)
                    reasons = ",".join(f"{k}:{v}" for k, v in sorted(plan.reasons.items())) or "-"
                    print(f"{verb} id={row_id} table={table} state={plan.state} patterns={','.join(names) or '-'} "
                          f"messages={plan.messages} withheld={plan.withheld} withheld_reasons={reasons} "
                          f"columns={','.join(plan.columns) or '-'}", file=out)
        if apply:
            busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            summary["checkpoint_incomplete"] = bool(busy and busy[0][0])
    finally:
        conn.close()
    if collect:
        summary["residue"] = _residue(ledger, needles)
    for key in ("patterns", "withheld_by_reason", "by_table"):
        summary[key] = dict(sorted(summary[key].items()))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ledger", required=True, type=Path, help="path of the capture ledger SQLite file")
    parser.add_argument("--apply", action="store_true", help="write the redactions (default: dry run)")
    parser.add_argument("--verify-residue", action="store_true",
                        help="after --apply, look for the original strings in the raw .db/-wal bytes")
    args = parser.parse_args(argv)
    if args.verify_residue and not args.apply:
        parser.error("--verify-residue needs --apply")
    try:
        summary = run(args.ledger, apply=args.apply, verify_residue=args.verify_residue)
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(f"error reason={type(exc).__name__}", file=sys.stderr)
        return 1
    print("summary " + json.dumps({"mode": "apply" if args.apply else "dry-run", **summary}, sort_keys=True))
    if summary["failed"]:
        return 1
    if args.apply and (summary["skipped_busy"] or summary["checkpoint_incomplete"] or summary["residue"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
