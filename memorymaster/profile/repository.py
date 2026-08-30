"""SQLite repository for resumable compiled-profile projection work."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from memorymaster.core.security import scan_persisted_value
from memorymaster.profile.models import (
    MessageBatch,
    ProfileCandidate,
    ProfileDecision,
    ProfileFact,
    ProfileMessage,
)
from memorymaster.stores._storage_shared import open_conn


_WRAPPER = re.compile(
    r"(?is)^\s*(?:<system-reminder|\[system|<task-notification|<local-command|stop hook feedback:)"
)


class ProfileRepository:
    """Persist only derived facts, exact support IDs, hashes, and run state."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    def connect(self) -> sqlite3.Connection:
        return open_conn(self.db_path)

    def max_user_id(self) -> int:
        with closing(self.connect()) as conn:
            if not self._has_table(conn, "verbatim_memories"):
                return 0
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM verbatim_memories WHERE role='user'"
            ).fetchone()
        return int(row[0]) if row else 0

    def active_run(self) -> dict[str, Any] | None:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM compiled_profile_runs WHERE active_slot=1 LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def latest_completed_run(self) -> dict[str, Any] | None:
        with closing(self.connect()) as conn:
            row = conn.execute(
                """SELECT * FROM compiled_profile_runs WHERE status='completed'
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row else None

    def due(self, *, now: datetime, cadence_days: int) -> bool:
        latest = self.latest_completed_run()
        if latest is None or not latest.get("completed_at"):
            return True
        completed = datetime.fromisoformat(str(latest["completed_at"]))
        return completed <= now - timedelta(days=max(1, cadence_days))

    def start_run(
        self, *, target: int, map_model: str, reduce_model: str, now: datetime
    ) -> dict[str, Any]:
        active = self.active_run()
        if active is not None:
            return active
        latest = self.latest_completed_run()
        start = int(latest["target_watermark"]) if latest else 0
        timestamp = now.isoformat()
        with closing(self.connect()) as conn:
            cur = conn.execute(
                """INSERT INTO compiled_profile_runs
                   (status, active_slot, start_watermark, current_watermark,
                    target_watermark, map_model, reduce_model, started_at, updated_at)
                   VALUES ('mapping',1,?,?,?,?,?,?,?)""",
                (start, start, target, map_model, reduce_model, timestamp, timestamp),
            )
            conn.commit()
            run_id = int(cur.lastrowid)
        return self.run(run_id)

    def run(self, run_id: int) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM compiled_profile_runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"compiled profile run {run_id} does not exist")
        return dict(row)

    def message_batch(
        self,
        *,
        after_id: int,
        through_id: int,
        max_messages: int,
        max_chars: int,
    ) -> MessageBatch:
        rows = self._message_rows(after_id, through_id, max_messages)
        messages: list[ProfileMessage] = []
        scanned = after_id
        used = 0
        for row in rows:
            message = self._profile_message(row)
            if message is None:
                scanned = int(row["id"])
                continue
            size = len(message.text) + len(message.assistant_context)
            if messages and used + size > max_chars:
                break
            messages.append(message)
            used += size
            scanned = int(row["id"])
            if len(messages) >= max_messages:
                break
        if not rows:
            scanned = through_id
        return MessageBatch(tuple(messages), scanned)

    def _message_rows(
        self, after_id: int, through_id: int, max_messages: int
    ) -> list[sqlite3.Row]:
        fetch_limit = max(100, max_messages * 10)
        with closing(self.connect()) as conn:
            if not self._has_table(conn, "verbatim_memories"):
                return []
            return conn.execute(
                """SELECT v.id, v.session_id, v.scope, v.content,
                          COALESCE((SELECT a.content FROM verbatim_memories a
                           WHERE a.session_id=v.session_id AND a.id<v.id
                             AND a.role='assistant' ORDER BY a.id DESC LIMIT 1),'') context
                   FROM verbatim_memories v
                   WHERE v.role='user' AND v.id>? AND v.id<=?
                   ORDER BY v.id LIMIT ?""",
                (after_id, through_id, fetch_limit),
            ).fetchall()

    @staticmethod
    def _profile_message(row: sqlite3.Row) -> ProfileMessage | None:
        text = str(row["content"] or "").strip()
        context = str(row["context"] or "")[-400:]
        if not text or _WRAPPER.search(text) or scan_persisted_value({"text": text, "context": context}):
            return None
        return ProfileMessage(
            int(row["id"]),
            str(row["session_id"]),
            str(row["scope"]),
            text[:16_000],
            context,
        )

    def save_mapping(
        self,
        run_id: int,
        candidates: tuple[ProfileCandidate, ...],
        scanned_through_id: int,
        *,
        now: datetime,
        provider_called: bool,
    ) -> None:
        timestamp = now.isoformat()
        with closing(self.connect()) as conn:
            for candidate in candidates:
                self._insert_candidate(conn, run_id, candidate, timestamp)
            conn.execute(
                """UPDATE compiled_profile_runs
                   SET current_watermark=?, map_calls=map_calls+?, updated_at=?
                   WHERE id=? AND status='mapping'""",
                (scanned_through_id, int(provider_called), timestamp, run_id),
            )
            conn.commit()

    @staticmethod
    def _insert_candidate(
        conn: sqlite3.Connection,
        run_id: int,
        candidate: ProfileCandidate,
        timestamp: str,
    ) -> None:
        material = json.dumps(
            [candidate.category, candidate.predicate, candidate.value, candidate.support_ids],
            ensure_ascii=False,
        )
        candidate_id = "pc-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        conn.execute(
            """INSERT OR IGNORE INTO compiled_profile_candidates
               (run_id, candidate_id, category, predicate, value, volatility,
                support_ids_json, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (
                run_id,
                candidate_id,
                candidate.category,
                candidate.predicate,
                candidate.value.strip(),
                candidate.volatility,
                json.dumps(candidate.support_ids),
                timestamp,
            ),
        )

    def mark_reducing(self, run_id: int, *, now: datetime) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "UPDATE compiled_profile_runs SET status='reducing', updated_at=? WHERE id=?",
                (now.isoformat(), run_id),
            )
            conn.commit()

    def candidates(
        self, run_id: int, *, pending_only: bool = False
    ) -> tuple[ProfileCandidate, ...]:
        """Candidatos del run; con ``pending_only`` solo los aun no consumidos.

        El reduce va por lotes y marca cada candidato consumido al aplicar su
        decision, asi que un run reanudado debe ver SOLO lo que falta. Sin ese
        filtro, reanudar volveria a aplicar lo ya aplicado.
        """
        where = "WHERE run_id=?" + (
            " AND consumed_at IS NULL" if pending_only else ""
        )
        with closing(self.connect()) as conn:
            rows = conn.execute(
                f"""SELECT * FROM compiled_profile_candidates
                   {where} ORDER BY candidate_id""",
                (run_id,),
            ).fetchall()
        return tuple(
            ProfileCandidate(
                str(row["candidate_id"]),
                str(row["category"]),
                str(row["predicate"]),
                str(row["value"]),
                str(row["volatility"]),
                tuple(json.loads(row["support_ids_json"])),
            )
            for row in rows
        )

    def active_facts(self) -> tuple[ProfileFact, ...]:
        return self._facts("status='active'", ())

    def fact(self, fact_id: int) -> ProfileFact:
        rows = self._facts("id=?", (fact_id,))
        if not rows:
            raise KeyError(f"compiled profile fact {fact_id} does not exist")
        return rows[0]

    def _facts(self, where: str, params: tuple[Any, ...]) -> tuple[ProfileFact, ...]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM compiled_profile_facts WHERE {where} ORDER BY id", params
            ).fetchall()
            supports = self._supports_by_fact(conn, [int(row["id"]) for row in rows])
        return tuple(self._fact_from_row(row, supports.get(int(row["id"]), ())) for row in rows)

    @staticmethod
    def _supports_by_fact(
        conn: sqlite3.Connection, fact_ids: list[int]
    ) -> dict[int, tuple[int, ...]]:
        if not fact_ids:
            return {}
        placeholders = ",".join("?" for _ in fact_ids)
        rows = conn.execute(
            f"""SELECT fact_id, verbatim_id FROM compiled_profile_supports
                WHERE fact_id IN ({placeholders}) ORDER BY fact_id, verbatim_id""",
            fact_ids,
        ).fetchall()
        grouped: dict[int, list[int]] = {}
        for row in rows:
            grouped.setdefault(int(row["fact_id"]), []).append(int(row["verbatim_id"]))
        return {key: tuple(value) for key, value in grouped.items()}

    @staticmethod
    def _fact_from_row(row: sqlite3.Row, supports: tuple[int, ...]) -> ProfileFact:
        return ProfileFact(
            fact_id=int(row["id"]),
            category=str(row["category"]),
            predicate=str(row["predicate"]),
            value=str(row["value"]),
            volatility=str(row["volatility"]),
            status=str(row["status"]),
            support_hash=str(row["support_hash"]),
            support_count=int(row["support_count"]),
            independent_sessions=int(row["independent_sessions"]),
            first_seen_at=str(row["first_seen_at"]),
            last_supported_at=str(row["last_supported_at"]),
            support_ids=supports,
        )

    def apply_decisions(
        self,
        run_id: int,
        decisions: tuple[ProfileDecision, ...],
        *,
        now: datetime,
        min_sessions: int,
    ) -> dict[str, int]:
        candidates = {item.candidate_id: item for item in self.candidates(run_id)}
        stats = {"applied": 0, "rejected": 0, "consumed": 0}
        stamp = now.isoformat()
        with closing(self.connect()) as conn:
            for decision in decisions:
                support_ids = self._decision_supports(decision, candidates)
                applied = self._apply_decision(
                    conn, decision, support_ids, now=now, min_sessions=min_sessions
                )
                stats["applied" if applied else "rejected"] += 1
                # Se marca DENTRO de la misma transaccion que aplico la decision.
                # Separarlo reabre la doble aplicacion: un commit del hecho sin el
                # marcado deja el candidato listo para volver a aplicarse.
                for candidate_id in decision.candidate_ids:
                    conn.execute(
                        """UPDATE compiled_profile_candidates SET consumed_at=?
                           WHERE run_id=? AND candidate_id=? AND consumed_at IS NULL""",
                        (stamp, run_id, candidate_id),
                    )
                    stats["consumed"] += 1
            conn.commit()
        return stats

    @staticmethod
    def _decision_supports(
        decision: ProfileDecision, candidates: dict[str, ProfileCandidate]
    ) -> tuple[int, ...]:
        support_ids = {
            support_id
            for candidate_id in decision.candidate_ids
            for support_id in candidates[candidate_id].support_ids
        }
        return tuple(sorted(support_ids))

    def _apply_decision(
        self,
        conn: sqlite3.Connection,
        decision: ProfileDecision,
        support_ids: tuple[int, ...],
        *,
        now: datetime,
        min_sessions: int,
    ) -> bool:
        if decision.action == "ignore":
            return True
        support_rows = self._support_rows(conn, support_ids)
        sessions = {str(row["session_id"]) for row in support_rows}
        if len(support_rows) != len(support_ids):
            return False
        if decision.action in {"add", "replace"} and len(sessions) < min_sessions:
            return False
        if decision.action == "reinforce":
            return self._reinforce(conn, int(decision.target_fact_id or 0), support_rows, now)
        fact_id = self._upsert_fact(conn, decision, support_rows, now)
        if decision.action == "replace":
            return self._supersede(conn, int(decision.target_fact_id or 0), fact_id, now)
        return bool(fact_id)

    @staticmethod
    def _support_rows(
        conn: sqlite3.Connection, support_ids: tuple[int, ...]
    ) -> list[sqlite3.Row]:
        if not support_ids:
            return []
        placeholders = ",".join("?" for _ in support_ids)
        return conn.execute(
            f"""SELECT id, session_id, content, timestamp FROM verbatim_memories
                WHERE role='user' AND id IN ({placeholders}) ORDER BY id""",
            support_ids,
        ).fetchall()

    def _upsert_fact(
        self,
        conn: sqlite3.Connection,
        decision: ProfileDecision,
        support_rows: list[sqlite3.Row],
        now: datetime,
    ) -> int:
        key = self._fact_key(decision.category, decision.predicate, decision.value)
        existing = conn.execute(
            "SELECT id FROM compiled_profile_facts WHERE fact_key=?", (key,)
        ).fetchone()
        if existing:
            fact_id = int(existing["id"])
            conn.execute(
                "UPDATE compiled_profile_facts SET status='active', updated_at=? WHERE id=?",
                (now.isoformat(), fact_id),
            )
        else:
            fact_id = self._insert_fact(conn, decision, key, support_rows, now)
        self._attach_supports(conn, fact_id, support_rows, now)
        self._refresh_support_stats(conn, fact_id, now)
        return fact_id

    @staticmethod
    def _insert_fact(
        conn: sqlite3.Connection,
        decision: ProfileDecision,
        key: str,
        support_rows: list[sqlite3.Row],
        now: datetime,
    ) -> int:
        timestamps = [str(row["timestamp"]) for row in support_rows]
        first_seen = min(timestamps) if timestamps else now.isoformat()
        last_seen = max(timestamps) if timestamps else now.isoformat()
        cur = conn.execute(
            """INSERT INTO compiled_profile_facts
               (fact_key, category, predicate, value, volatility, status,
                support_hash, support_count, independent_sessions, first_seen_at,
                last_supported_at, created_at, updated_at)
               VALUES (?,?,?,?,?,'active',?,0,0,?,?,?,?)""",
            (
                key,
                decision.category,
                decision.predicate,
                decision.value.strip(),
                decision.volatility,
                hashlib.sha256(b"").hexdigest(),
                first_seen,
                last_seen,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def _attach_supports(
        conn: sqlite3.Connection,
        fact_id: int,
        rows: Iterable[sqlite3.Row],
        now: datetime,
    ) -> None:
        for row in rows:
            content_hash = hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest()
            conn.execute(
                """INSERT OR IGNORE INTO compiled_profile_supports
                   (fact_id, verbatim_id, session_id, message_hash, supported_at, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    fact_id,
                    int(row["id"]),
                    str(row["session_id"]),
                    content_hash,
                    str(row["timestamp"]),
                    now.isoformat(),
                ),
            )

    @staticmethod
    def _refresh_support_stats(
        conn: sqlite3.Connection, fact_id: int, now: datetime
    ) -> None:
        rows = conn.execute(
            """SELECT verbatim_id, session_id, message_hash, supported_at
               FROM compiled_profile_supports WHERE fact_id=? ORDER BY verbatim_id""",
            (fact_id,),
        ).fetchall()
        material = "|".join(f"{row['verbatim_id']}:{row['message_hash']}" for row in rows)
        support_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        sessions = len({str(row["session_id"]) for row in rows})
        last_seen = max(str(row["supported_at"]) for row in rows)
        conn.execute(
            """UPDATE compiled_profile_facts
               SET support_hash=?, support_count=?, independent_sessions=?,
                   last_supported_at=?, updated_at=? WHERE id=?""",
            (support_hash, len(rows), sessions, last_seen, now.isoformat(), fact_id),
        )

    def _reinforce(
        self,
        conn: sqlite3.Connection,
        fact_id: int,
        rows: list[sqlite3.Row],
        now: datetime,
    ) -> bool:
        target = conn.execute(
            "SELECT id FROM compiled_profile_facts WHERE id=? AND status='active'", (fact_id,)
        ).fetchone()
        if target is None:
            return False
        self._attach_supports(conn, fact_id, rows, now)
        self._refresh_support_stats(conn, fact_id, now)
        return True

    @staticmethod
    def _supersede(
        conn: sqlite3.Connection, target_id: int, replacement_id: int, now: datetime
    ) -> bool:
        if target_id == replacement_id:
            return False
        target = conn.execute(
            "SELECT id FROM compiled_profile_facts WHERE id=? AND status='active'", (target_id,)
        ).fetchone()
        if target is None:
            return False
        conn.execute(
            """UPDATE compiled_profile_facts
               SET status='superseded', replaced_by_fact_id=?, updated_at=? WHERE id=?""",
            (replacement_id, now.isoformat(), target_id),
        )
        return True

    def expire_preferences(self, *, now: datetime, ttl_days: int) -> int:
        threshold = (now - timedelta(days=max(1, ttl_days))).isoformat()
        with closing(self.connect()) as conn:
            cur = conn.execute(
                """UPDATE compiled_profile_facts SET status='expired', updated_at=?
                   WHERE status='active' AND volatility='preference'
                     AND last_supported_at<?""",
                (now.isoformat(), threshold),
            )
            conn.commit()
            return int(cur.rowcount)

    def complete_run(self, run_id: int, output_hash: str, *, now: datetime) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                """UPDATE compiled_profile_runs
                   SET status='completed', active_slot=NULL, output_hash=?,
                       updated_at=?, completed_at=? WHERE id=?""",
                (output_hash, now.isoformat(), now.isoformat(), run_id),
            )
            conn.commit()

    def record_error(self, run_id: int, code: str, *, now: datetime) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "UPDATE compiled_profile_runs SET error_code=?, updated_at=? WHERE id=?",
                (code[:120], now.isoformat(), run_id),
            )
            conn.commit()

    def insert_fact_for_test(
        self,
        *,
        category: str,
        predicate: str,
        value: str,
        volatility: str,
        last_supported_at: datetime,
        support_count: int = 1,
    ) -> int:
        key = self._fact_key(category, predicate, value)
        timestamp = last_supported_at.isoformat()
        with closing(self.connect()) as conn:
            cur = conn.execute(
                """INSERT INTO compiled_profile_facts
                   (fact_key, category, predicate, value, volatility, status,
                    support_hash, support_count, independent_sessions, first_seen_at,
                    last_supported_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,'active',?,?,?,?,?,?,?)""",
                (
                    key,
                    category,
                    predicate,
                    value,
                    volatility,
                    hashlib.sha256(key.encode()).hexdigest(),
                    support_count,
                    support_count,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    @staticmethod
    def _fact_key(category: str, predicate: str, value: str) -> str:
        normalized = " ".join(value.lower().split())
        return hashlib.sha256(f"{category}|{predicate}|{normalized}".encode("utf-8")).hexdigest()

    @staticmethod
    def _has_table(conn: sqlite3.Connection, table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None


__all__ = ["ProfileRepository"]
