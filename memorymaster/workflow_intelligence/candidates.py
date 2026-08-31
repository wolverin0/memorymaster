"""Deterministic correction clustering and inert intervention proposals."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from pathlib import Path

from .redaction import public_excerpt
from .storage import WorkflowStore, utc_now


_DESTINATIONS = {
    "research_before_editing": "WORKFLOW",
    "verification_missing": "TEST",
    "instruction_ignored": "GLOBAL RULE",
    "scope_misunderstood": "WORKFLOW",
    "overengineering": "GLOBAL RULE",
    "premature_stop": "WORKFLOW",
    "redirection": "MEMORY",
}


def refresh_candidates(store: WorkflowStore) -> dict[str, int]:
    rows = store.connection.execute(
        """SELECT f.feedback_id,f.session_id,f.theme,f.excerpt,s.project_scope
           FROM feedback f JOIN sessions s ON s.session_id=f.session_id
           WHERE f.user_origin=1 AND s.session_kind IN ('human','mixed')
           ORDER BY f.theme,f.session_id"""
    ).fetchall()
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for row in rows:
        groups[(row["theme"], row["project_scope"])].append(row)
        groups[(row["theme"], "user")].append(row)
    created = updated = 0
    for (theme, scope), evidence in groups.items():
        sessions = {row["session_id"] for row in evidence}
        projects = {row["project_scope"] for row in evidence}
        destination = _DESTINATIONS.get(theme, "NO ACTION")
        fingerprint = hashlib.sha256(f"{theme}|{scope}|{destination}".encode()).hexdigest()
        candidate_id = "candidate-" + fingerprint[:16]
        now = utc_now()
        exists = store.connection.execute(
            "SELECT 1 FROM candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        recurrent = len(sessions) >= 3
        cross_project = scope != "user" or len(projects) >= 2
        status = "proposed" if recurrent and cross_project else "watch"
        excerpt = public_excerpt(evidence[0]["excerpt"])
        store.connection.execute(
            """INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(candidate_id) DO UPDATE SET
                 status=CASE WHEN candidates.status='reviewed' THEN candidates.status ELSE excluded.status END,
                 confidence=excluded.confidence,support_count=excluded.support_count,
                 project_count=excluded.project_count,updated_at=excluded.updated_at""",
            (candidate_id, fingerprint, destination, status, scope, theme, theme.replace("_", " "),
             excerpt, min(0.95, 0.4 + len(sessions) * 0.1), len(sessions), len(projects), now, now),
        )
        store.connection.execute("DELETE FROM candidate_supports WHERE candidate_id=?", (candidate_id,))
        store.connection.executemany(
            """INSERT INTO candidate_supports VALUES (?,?,?,?,?,?)""",
            [(candidate_id, row["session_id"], row["feedback_id"], row["project_scope"],
              f"{row['session_id'][:12]}:{row['feedback_id'][:12]}",
              hashlib.sha256(row["excerpt"].encode()).hexdigest()) for row in evidence],
        )
        created += int(exists is None)
        updated += int(exists is not None)
    store.connection.commit()
    return {"created": created, "updated": updated, "groups": len(groups)}


def review_candidate(
    store: WorkflowStore, candidate_id: str, decision: str, *, rationale: str = "",
) -> dict[str, str]:
    allowed = {"accept_pattern", "reject_noise", "watch", "relabel"}
    if decision not in allowed:
        raise ValueError("unsupported review decision")
    row = store.connection.execute(
        "SELECT candidate_id FROM candidates WHERE candidate_id=?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise ValueError("candidate does not exist")
    store.connection.execute(
        "INSERT INTO reviews(candidate_id,decision,reviewer,rationale_excerpt,reviewed_at) VALUES (?,?,?,?,?)",
        (candidate_id, decision, "human", public_excerpt(rationale), utc_now()),
    )
    status = "reviewed" if decision != "watch" else "watch"
    store.connection.execute(
        "UPDATE candidates SET status=?,updated_at=? WHERE candidate_id=?",
        (status, utc_now(), candidate_id),
    )
    store.connection.commit()
    return {"candidate_id": candidate_id, "decision": decision, "status": status}


def write_proposal(store: WorkflowStore, candidate_id: str, output: str | Path) -> Path:
    row = store.connection.execute(
        "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise ValueError("candidate does not exist")
    support = store.connection.execute(
        "SELECT session_id,project_scope,source_ref,evidence_hash FROM candidate_supports WHERE candidate_id=?",
        (candidate_id,),
    ).fetchall()
    payload = {
        "schema_version": "memorymaster.workflow-proposal.v1",
        "proposal_id": "proposal-" + uuid.uuid4().hex,
        "inert": True,
        "candidate": {key: row[key] for key in row.keys()},
        "supports": [dict(item) for item in support],
        "promotion": {"automatic": False, "required_surface": "human-governed review"},
    }
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


__all__ = ["refresh_candidates", "review_candidate", "write_proposal"]
