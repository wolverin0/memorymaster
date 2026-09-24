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
from memorymaster.core.temporal_policy import claim_is_temporally_current
from memorymaster.profile.egress import prepare_claim_egress
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

# Governed claims usable as profile evidence when verbatim input is empty
# (review F-03): confirmed claims, plus claims Dreaming applied that still
# await the steward. Public only; stale/superseded/archived never qualify.
# Claim supports are stored with the claim id NEGATED in verbatim_id, so the
# exact-support manifest, its primary key and the mismatch check are shared.
_CLAIM_ELIGIBLE_SQL = (
    "(status = 'confirmed' OR (status = 'candidate' AND source_agent = 'dream-worker')) "
    "AND COALESCE(visibility, 'public') = 'public' AND replaced_by_claim_id IS NULL"
)
# Tenant boundary, same semantics as the store's ``tenant_id IS ?`` lookups:
# the service tenant's rows only, legacy NULL-tenant rows only when the
# service tenant is unset. Applies to selection and to support resolution.
_CLAIM_TENANT_SQL = "tenant_id IS ?"
# A claims run takes the eligible claims no earlier run has mapped. Not a
# MAX(id) watermark: a claim becomes eligible long after higher ids were
# compiled (steward confirmation, S1 re-confirming a stale claim, a conflict
# resolved), and Dreaming candidates are eligible the moment they exist.
_CLAIM_UNSEEN_SQL = (
    "NOT EXISTS (SELECT 1 FROM compiled_profile_claim_seen seen WHERE seen.claim_id = claims.id)"
)


class _Validity:
    __slots__ = ("valid_from", "valid_until")

    def __init__(self, valid_from: Any, valid_until: Any) -> None:
        self.valid_from = valid_from
        self.valid_until = valid_until


def _claim_citations(conn: sqlite3.Connection, claim_ids: list[int]) -> dict[int, list[tuple[str, str]]]:
    if not claim_ids:
        return {}
    placeholders = ",".join("?" for _ in claim_ids)
    grouped: dict[int, list[tuple[str, str]]] = {}
    for row in conn.execute(
        f"SELECT claim_id, source, locator FROM citations WHERE claim_id IN ({placeholders}) ORDER BY id",
        claim_ids,
    ):
        grouped.setdefault(int(row["claim_id"]), []).append((str(row["source"] or ""), str(row["locator"] or "")))
    return grouped


def _claim_session(claim_id: int, citations: list[tuple[str, str]]) -> str:
    """Independent-evidence unit of a claim: its session lineage.

    Claims with no session or Dreaming lineage all share ``claim:unlinked``,
    so they never satisfy the independent-sessions gate by themselves. A claim
    citing several sessions is its own unit.
    """
    from memorymaster.recall.retrieval import _normalize_session_locator

    for source, locator in citations:
        # Dreaming locator: dream:<provider>:<session hash>:<message id>
        if source == "dream-worker" and locator.startswith("dream:") and locator.count(":") >= 3:
            return locator.rsplit(":", 1)[0]
    sessions = {
        _normalize_session_locator(locator)
        for source, locator in citations
        if source == "session" and locator.strip()
    }
    if len(sessions) == 1:
        return "session:" + sessions.pop()
    if not sessions:
        return "claim:unlinked"
    return f"claim:{claim_id}"


def _claim_usable(text: str, valid_from: Any, valid_until: Any) -> bool:
    # A redaction marker means the claim carried a secret at ingest; like
    # is_sensitive_claim, treat it as sensitive rather than as clean text.
    # redact_claim_payload leaves status and visibility as they were, so its
    # [REDACTED_CLAIM_TEXT] / [ERASED_CLAIM_TEXT] payloads are caught here.
    # The egress check keeps a claim the map provider may not receive out of
    # selection too, so it never stays pending.
    return (
        bool(text)
        and "[REDACTED" not in text
        and "[ERASED_CLAIM_TEXT]" not in text
        and not scan_persisted_value({"text": text})
        and claim_is_temporally_current(_Validity(valid_from, valid_until))
        and not prepare_claim_egress(text).blocked
    )


def _eligible_claim_rows(
    conn: sqlite3.Connection, tenant_id: str | None, where: str, params: list[Any]
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""SELECT id, scope, text, created_at, valid_from, valid_until FROM claims
            WHERE {_CLAIM_ELIGIBLE_SQL} AND {_CLAIM_TENANT_SQL} AND {where}""",
        [tenant_id, *params],
    ).fetchall()
    citations = _claim_citations(conn, [int(row["id"]) for row in rows])
    out: list[dict[str, Any]] = []
    for row in rows:
        text = str(row["text"] or "").strip()
        usable = _claim_usable(text, row["valid_from"], row["valid_until"])
        out.append({
            "id": int(row["id"]),
            "scope": str(row["scope"] or ""),
            "content": text,
            "timestamp": str(row["created_at"]),
            "session_id": _claim_session(int(row["id"]), citations.get(int(row["id"]), [])),
            "usable": usable,
        })
    return out


class ProfileRepository:
    """Persist only derived facts, exact support IDs, hashes, and run state."""

    def __init__(self, db_path: str | Path, *, tenant_id: str | None = None) -> None:
        self.db_path = str(db_path)
        # Same normalization as MemoryService.tenant_id.
        self.tenant_id = (tenant_id or "").strip() or None

    def connect(self) -> sqlite3.Connection:
        return open_conn(self.db_path)

    def pending_claim_bounds(self) -> tuple[int, int] | None:
        """Lowest and highest id of usable eligible claims not mapped yet.

        ``None`` when no claim is pending, so an eligible claim that is not
        usable (secret finding, not current) never starts an empty run.
        """
        with closing(self.connect()) as conn:
            rows = conn.execute(
                f"""SELECT id, text, valid_from, valid_until FROM claims
                    WHERE {_CLAIM_ELIGIBLE_SQL} AND {_CLAIM_TENANT_SQL} AND {_CLAIM_UNSEEN_SQL}
                    ORDER BY id""",
                (self.tenant_id,),
            ).fetchall()
        ids = [
            int(row["id"])
            for row in rows
            if _claim_usable(str(row["text"] or "").strip(), row["valid_from"], row["valid_until"])
        ]
        return (ids[0], ids[-1]) if ids else None

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

    def latest_completed_run(self, source: str | None = None) -> dict[str, Any] | None:
        """Latest completed run, optionally of one evidence ``source``.

        Verbatim runs continue from the latest verbatim run's target; claims
        runs select the claims not mapped yet (``pending_claim_bounds``).
        """
        where = "status='completed'" + (" AND source=?" if source else "")
        with closing(self.connect()) as conn:
            row = conn.execute(
                f"SELECT * FROM compiled_profile_runs WHERE {where} ORDER BY id DESC LIMIT 1",
                (source,) if source else (),
            ).fetchone()
        return dict(row) if row else None

    def due(self, *, now: datetime, cadence_days: int) -> bool:
        latest = self.latest_completed_run()
        if latest is None or not latest.get("completed_at"):
            return True
        completed = datetime.fromisoformat(str(latest["completed_at"]))
        return completed <= now - timedelta(days=max(1, cadence_days))

    def start_run(
        self,
        *,
        target: int,
        map_model: str,
        reduce_model: str,
        now: datetime,
        source: str = "verbatim",
        start: int | None = None,
    ) -> dict[str, Any]:
        active = self.active_run()
        if active is not None:
            return active
        if start is None:
            latest = self.latest_completed_run(source)
            start = int(latest["target_watermark"]) if latest else 0
        timestamp = now.isoformat()
        with closing(self.connect()) as conn:
            cur = conn.execute(
                """INSERT INTO compiled_profile_runs
                   (status, active_slot, start_watermark, current_watermark,
                    target_watermark, map_model, reduce_model, started_at, updated_at, source)
                   VALUES ('mapping',1,?,?,?,?,?,?,?,?)""",
                (start, start, target, map_model, reduce_model, timestamp, timestamp, source),
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
        source: str = "verbatim",
    ) -> MessageBatch:
        if source == "claims":
            rows: list[Any] = self._claim_rows(after_id, through_id, max_messages)
            to_message = self._claim_message
        else:
            rows = self._message_rows(after_id, through_id, max_messages)
            to_message = self._profile_message
        messages: list[ProfileMessage] = []
        scanned = after_id
        used = 0
        for row in rows:
            message = to_message(row)
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

    def _claim_rows(
        self, after_id: int, through_id: int, max_messages: int
    ) -> list[dict[str, Any]]:
        fetch_limit = max(100, max_messages * 10)
        with closing(self.connect()) as conn:
            return _eligible_claim_rows(
                conn,
                self.tenant_id,
                f"id>? AND id<=? AND {_CLAIM_UNSEEN_SQL} ORDER BY id LIMIT ?",
                [after_id, through_id, fetch_limit],
            )

    @staticmethod
    def _claim_message(row: dict[str, Any]) -> ProfileMessage | None:
        if not row["usable"]:
            return None
        # Only the egress-redacted text leaves for the map provider.
        return ProfileMessage(
            -int(row["id"]),
            str(row["session_id"]),
            str(row["scope"]),
            prepare_claim_egress(str(row["content"])).text[:16_000],
            "",
        )

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
        seen_claim_ids: Iterable[int] = (),
    ) -> None:
        timestamp = now.isoformat()
        with closing(self.connect()) as conn:
            for candidate in candidates:
                self._insert_candidate(conn, run_id, candidate, timestamp)
            # Same transaction as the candidates and the watermark: a claim is
            # recorded as mapped only together with what its mapping produced.
            conn.executemany(
                """INSERT OR IGNORE INTO compiled_profile_claim_seen
                   (claim_id, run_id, seen_at) VALUES (?,?,?)""",
                [(int(claim_id), run_id, timestamp) for claim_id in seen_claim_ids],
            )
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

    def _support_rows(
        self, conn: sqlite3.Connection, support_ids: tuple[int, ...]
    ) -> list[Any]:
        """Resolve exact supports; a negative id is a claim that must still qualify."""
        verbatim_ids = [item for item in support_ids if item > 0]
        claim_ids = [-item for item in support_ids if item < 0]
        rows: list[Any] = []
        if verbatim_ids:
            placeholders = ",".join("?" for _ in verbatim_ids)
            rows.extend(conn.execute(
                f"""SELECT id, session_id, content, timestamp FROM verbatim_memories
                    WHERE role='user' AND id IN ({placeholders}) ORDER BY id""",
                verbatim_ids,
            ).fetchall())
        if claim_ids:
            placeholders = ",".join("?" for _ in claim_ids)
            rows.extend(
                {**row, "id": -int(row["id"])}
                for row in _eligible_claim_rows(
                    conn, self.tenant_id, f"id IN ({placeholders})", claim_ids
                )
                if row["usable"]
            )
        return sorted(rows, key=lambda row: int(row["id"]))

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
        # A retraction can leave a retired fact with no support at all.
        last_seen = max((str(row["supported_at"]) for row in rows), default=None)
        conn.execute(
            """UPDATE compiled_profile_facts
               SET support_hash=?, support_count=?, independent_sessions=?,
                   last_supported_at=COALESCE(?, last_supported_at), updated_at=? WHERE id=?""",
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

    def retract_ineligible_claim_supports(
        self, *, now: datetime, min_sessions: int
    ) -> dict[str, int]:
        """Re-check the claim supports of active facts (review F-03).

        A support whose claim is no longer eligible (status, visibility,
        replacement, tenant, currency, egress) is removed, and so is one whose
        claim was edited in place: its current text no longer hashes to the
        support's ``message_hash`` (ruling R4). A fact whose remaining support
        spans fewer than ``min_sessions`` sessions is retired as ``expired``. A
        removed claim leaves the seen set, so it is mapped again if it becomes
        eligible again (e.g. S1 re-confirms it) or with its edited text.
        """
        stats = {"supports_removed": 0, "facts_retired": 0}
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """SELECT s.fact_id, s.verbatim_id, s.message_hash FROM compiled_profile_supports s
                   JOIN compiled_profile_facts f ON f.id = s.fact_id
                   WHERE f.status = 'active' AND s.verbatim_id < 0"""
            ).fetchall()
            claim_ids = sorted({-int(row["verbatim_id"]) for row in rows})
            # Eligible claim id -> hash of its current text, as _attach_supports hashed it.
            eligible: dict[int, str] = {}
            for start in range(0, len(claim_ids), 500):
                chunk = claim_ids[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                eligible.update(
                    (int(row["id"]), hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest())
                    for row in _eligible_claim_rows(
                        conn, self.tenant_id, f"id IN ({placeholders})", chunk
                    )
                    if row["usable"]
                )
            removed = [
                (int(row["fact_id"]), int(row["verbatim_id"]))
                for row in rows
                if eligible.get(-int(row["verbatim_id"])) != str(row["message_hash"] or "")
            ]
            if not removed:
                return stats
            conn.executemany(
                "DELETE FROM compiled_profile_supports WHERE fact_id=? AND verbatim_id=?", removed
            )
            conn.executemany(
                "DELETE FROM compiled_profile_claim_seen WHERE claim_id=?",
                [(-support_id,) for support_id in sorted({item for _, item in removed})],
            )
            stamp = now.isoformat()
            for fact_id in sorted({fact for fact, _ in removed}):
                sessions = conn.execute(
                    "SELECT COUNT(DISTINCT session_id) FROM compiled_profile_supports WHERE fact_id=?",
                    (fact_id,),
                ).fetchone()[0]
                if int(sessions) < min_sessions:
                    conn.execute(
                        "UPDATE compiled_profile_facts SET status='expired', updated_at=? WHERE id=?",
                        (stamp, fact_id),
                    )
                    stats["facts_retired"] += 1
                self._refresh_support_stats(conn, fact_id, now)
            conn.commit()
        stats["supports_removed"] = len(removed)
        return stats

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
