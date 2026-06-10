"""Append-only JSONL write spool (P1 WAL-discipline spec §2.2/§2.3).

High-frequency ambient writers (recall-hook access/feedback records, Stop-hook
verbatim/learnings, dream bridge) append envelope lines here instead of opening
the multi-GB SQLite DB per event; the steward drains them through the normal
service paths (``jobs/spool_drain.py``). This file format is the only "wire
protocol" in the P1 design:

    {"v": 1, "op": "access"|"feedback"|"ingest"|"verbatim"|"dream",
     "ts": <iso8601>, "idempotency_key": <str|null>, "payload": {...}}

Layout (spec §2.2):

- Root ``~/.memorymaster/spool/`` (override: ``MEMORYMASTER_SPOOL_DIR``) —
  deliberately OUTSIDE the OneDrive-synced tree and outside the DB directory.
- Per-DB subdir ``<db-name>-<path-hash8>/`` — the hash suffix (beyond the
  spec's literal ``<db-name>``) prevents two DBs that share a filename from
  draining into each other, which would be silent cross-DB write corruption.
- One file per writer-process per day (``{pid}-{date}.jsonl``), opened in
  append mode (``O_APPEND``) with single-write lines ≤4 KB — atomic on NTFS
  for practical purposes.
- The drainer RENAMES a file before reading it (``claim_files``) so writers
  never race the reader; unparseable/unknown lines are preserved under
  ``quarantine/``, never dropped silently.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

ENV_SPOOL_DIR = "MEMORYMASTER_SPOOL_DIR"

SPOOL_VERSION = 1
KNOWN_OPS = ("access", "feedback", "ingest", "verbatim", "dream")

DRAINING_SUFFIX = ".draining"
QUARANTINE_DIRNAME = "quarantine"


def spool_root() -> Path:
    """Spool base dir: ``MEMORYMASTER_SPOOL_DIR`` or ``~/.memorymaster/spool``."""
    env = os.environ.get(ENV_SPOOL_DIR, "").strip()
    if env:
        return Path(env)
    return Path.home() / ".memorymaster" / "spool"


def spool_dir_for(db_path: str | Path) -> Path:
    """Per-DB spool dir: ``<root>/<db-name>-<path-hash8>/``.

    The 8-char hash of the resolved DB path keeps two DBs with the same
    filename (ubiquitous: every checkout names it ``memorymaster.db``) from
    sharing a spool — a drain would otherwise replay one DB's writes into
    the other.
    """
    path = Path(db_path)
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:8]
    return spool_root() / f"{path.name}-{digest}"


def quarantine_dir_for(db_path: str | Path) -> Path:
    """Quarantine subfolder for lines the drainer refuses to replay."""
    return spool_dir_for(db_path) / QUARANTINE_DIRNAME


def make_envelope(
    op: str,
    payload: dict[str, object],
    *,
    idempotency_key: str | None = None,
    ts: str | None = None,
) -> dict[str, object]:
    """Build a v1 spool envelope (the wire protocol, spec §2.2)."""
    return {
        "v": SPOOL_VERSION,
        "op": op,
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "idempotency_key": idempotency_key,
        "payload": payload,
    }


def append(
    db_path: str | Path,
    op: str,
    payload: dict[str, object],
    *,
    idempotency_key: str | None = None,
    ts: str | None = None,
) -> Path:
    """Append one envelope line to this process's daily spool file.

    Append mode == ``O_APPEND``; the envelope is written as a single
    ``write()`` so concurrent appenders (multiple panes, hooks) interleave
    at line granularity, not byte granularity. Returns the file written.
    """
    if op not in KNOWN_OPS:
        raise ValueError(f"unknown spool op: {op!r} (known: {KNOWN_OPS})")
    envelope = make_envelope(op, payload, idempotency_key=idempotency_key, ts=ts)
    line = json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
    spool_dir = spool_dir_for(db_path)
    spool_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    target = spool_dir / f"{os.getpid()}-{day}.jsonl"
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return target


def claim_files(db_path: str | Path) -> list[Path]:
    """Rename-before-read: take ownership of every pending spool file.

    Renames each ``*.jsonl`` to ``*.jsonl.<token>.draining`` so writers
    (which append to the original name) can never race the reader — a writer
    that appends after the rename simply creates a fresh file for the next
    drain. Leftover ``.draining`` files from a crashed drain are re-claimed
    first: replay is idempotent (spec §2.4), so re-reading them is safe.
    A file that cannot be renamed (writer mid-append on Windows) is skipped
    this round, never lost.
    """
    spool_dir = spool_dir_for(db_path)
    if not spool_dir.exists():
        return []
    claimed = sorted(spool_dir.glob(f"*{DRAINING_SUFFIX}"))
    token = f"{os.getpid()}-{time.time_ns()}"
    for path in sorted(spool_dir.glob("*.jsonl")):
        target = path.with_name(f"{path.name}.{token}{DRAINING_SUFFIX}")
        try:
            path.rename(target)
        except OSError:
            continue
        claimed.append(target)
    return claimed


def read_lines(path: Path) -> list[str]:
    """Raw non-empty lines of a claimed spool file (decode errors replaced)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()]


def quarantine_line(db_path: str | Path, raw_line: str, reason: str) -> Path:
    """Preserve an unreplayable line under ``quarantine/`` — never drop it.

    Wrapped with the reason + timestamp so the operator can audit and
    hand-replay after a fix; the raw line is kept byte-for-byte.
    """
    qdir = quarantine_dir_for(db_path)
    qdir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    target = qdir / f"{day}.jsonl"
    record = json.dumps(
        {
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "raw": raw_line,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(record + "\n")
    return target


def pending_depth(db_path: str | Path) -> dict[str, int]:
    """Spool depth metric for §2.10 observability: pending files + lines.

    Counts both unclaimed ``*.jsonl`` and crashed-drain ``*.draining``
    leftovers — everything the next drain would pick up.
    """
    spool_dir = spool_dir_for(db_path)
    if not spool_dir.exists():
        return {"files": 0, "lines": 0}
    files = sorted(spool_dir.glob("*.jsonl")) + sorted(spool_dir.glob(f"*{DRAINING_SUFFIX}"))
    lines = sum(len(read_lines(path)) for path in files)
    return {"files": len(files), "lines": lines}
