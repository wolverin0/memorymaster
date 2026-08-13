"""Deterministic discovery and fail-closed validation for graph observations."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from memorymaster.core.security import scan_text_for_findings


ALGORITHM_VERSION = "graph-observations-union-find-v1"
OBSERVATION_TYPES = frozenset(
    {
        "decision",
        "commitment",
        "constraint",
        "dependency",
        "state_change",
        "recurring_pattern",
        "stable_relationship",
        "root_cause",
    }
)
MAX_CLAIMS = 20
MAX_EVIDENCE = 20
MAX_EDGES = 40
MAX_HUB_EPISODES = 20

Signature = tuple[int, str, int, str]


@dataclass(frozen=True, slots=True)
class ObservationSupport:
    claim_id: int
    evidence_id: int
    source_item_id: int
    source_entity_id: int
    relation: str
    target_entity_id: int
    ontology_version: str
    scope: str
    tenant_id: str | None
    confidence: float
    occurred_at: str | None = None

    @property
    def signature(self) -> Signature:
        return (
            self.source_entity_id,
            self.relation,
            self.target_entity_id,
            self.ontology_version,
        )


@dataclass(frozen=True, slots=True)
class ObservationComponent:
    scope: str
    tenant_id: str | None
    supports: tuple[ObservationSupport, ...]
    claim_ids: tuple[int, ...]
    evidence_ids: tuple[int, ...]
    source_item_ids: tuple[int, ...]
    signatures: tuple[Signature, ...]
    support_hash: str
    evidence_window_start: str | None
    evidence_window_end: str | None


@dataclass(frozen=True, slots=True)
class DiscoveryDiagnostic:
    code: str
    evidence_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    components: tuple[ObservationComponent, ...]
    diagnostics: tuple[DiscoveryDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class ObservationAssertion:
    text: str
    supporting_claim_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ObservationDraft:
    decision: str
    name: str = ""
    observation_type: str = ""
    summary: str = ""
    assertions: tuple[ObservationAssertion, ...] = ()


class ObservationOutputError(ValueError):
    """Raised when provider output violates the evidence-bound schema."""


class _UnionFind:
    def __init__(self, values: Iterable[int]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: int) -> int:
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != value:
            value, self.parent[value] = self.parent[value], root
        return root

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        low, high = sorted((left_root, right_root))
        self.parent[high] = low


def canonical_signature(
    source_entity_id: int,
    relation: str,
    target_entity_id: int,
    ontology_version: str,
    *,
    symmetric_relations: frozenset[str] = frozenset(),
) -> Signature:
    """Return an exact signature, sorting endpoints only for symmetric relations."""
    source, target = int(source_entity_id), int(target_entity_id)
    normalized_relation = str(relation).strip().lower()
    version = str(ontology_version).strip()
    if not normalized_relation or not version or source <= 0 or target <= 0:
        raise ValueError("malformed graph signature")
    if normalized_relation in symmetric_relations and source > target:
        source, target = target, source
    return source, normalized_relation, target, version


def support_fingerprint(
    supports: Iterable[ObservationSupport],
    *,
    algorithm_version: str = ALGORITHM_VERSION,
) -> str:
    manifest = [
        [
            row.claim_id,
            row.evidence_id,
            row.source_item_id,
            *row.signature,
        ]
        for row in supports
    ]
    payload = {
        "algorithm_version": algorithm_version,
        "ontology_versions": sorted({row.ontology_version for row in supports}),
        "supports": sorted(manifest),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _episode_signatures(
    supports: tuple[ObservationSupport, ...],
) -> dict[int, set[Signature]]:
    episodes: dict[int, set[Signature]] = defaultdict(set)
    for row in supports:
        episodes[row.evidence_id].add(row.signature)
    return episodes


def _component_groups(
    episodes: Mapping[int, set[Signature]],
) -> tuple[dict[int, list[int]], set[Signature]]:
    signature_episodes: dict[Signature, list[int]] = defaultdict(list)
    for evidence_id, signatures in episodes.items():
        for signature in signatures:
            signature_episodes[signature].append(evidence_id)
    hubs = {sig for sig, ids in signature_episodes.items() if len(ids) > MAX_HUB_EPISODES}
    union = _UnionFind(episodes)
    for signature in sorted(signature_episodes):
        ids = sorted(signature_episodes[signature])
        if signature in hubs or not ids:
            continue
        for evidence_id in ids[1:]:
            union.union(ids[0], evidence_id)
    groups: dict[int, list[int]] = defaultdict(list)
    for evidence_id in sorted(episodes):
        groups[union.find(evidence_id)].append(evidence_id)
    return groups, hubs


def _build_component(
    supports: tuple[ObservationSupport, ...],
    evidence_ids: set[int],
    hubs: set[Signature],
) -> tuple[ObservationComponent | None, str | None]:
    rows = tuple(
        sorted(
            (row for row in supports if row.evidence_id in evidence_ids and row.signature not in hubs),
            key=lambda row: (
                row.evidence_id,
                row.claim_id,
                row.source_item_id,
                row.signature,
            ),
        )
    )
    claims = tuple(sorted({row.claim_id for row in rows}))
    evidence = tuple(sorted({row.evidence_id for row in rows}))
    sources = tuple(sorted({row.source_item_id for row in rows}))
    signatures = tuple(sorted({row.signature for row in rows}))
    edges = {(row.claim_id, row.signature) for row in rows}
    if len(claims) < 3 or len(evidence) < 2 or len(signatures) < 2:
        return None, "below_eligibility_threshold"
    if len(claims) > MAX_CLAIMS or len(evidence) > MAX_EVIDENCE or len(edges) > MAX_EDGES:
        return None, "component_oversized"
    dates = sorted(row.occurred_at for row in rows if row.occurred_at)
    component = ObservationComponent(
        scope=rows[0].scope,
        tenant_id=rows[0].tenant_id,
        supports=rows,
        claim_ids=claims,
        evidence_ids=evidence,
        source_item_ids=sources,
        signatures=signatures,
        support_hash=support_fingerprint(rows),
        evidence_window_start=dates[0] if dates else None,
        evidence_window_end=dates[-1] if dates else None,
    )
    return component, None


def discover_components(
    supports: Iterable[ObservationSupport],
    *,
    scope: str,
    tenant_id: str | None,
) -> DiscoveryResult:
    """Build deterministic per-scope components; no model influences membership."""
    rows = tuple(
        row for row in supports if row.scope == scope and row.tenant_id == tenant_id
    )
    if not rows:
        return DiscoveryResult((), ())
    episodes = _episode_signatures(rows)
    groups, hubs = _component_groups(episodes)
    diagnostics = [
        DiscoveryDiagnostic("hub_signature_suppressed", tuple(sorted(episodes)))
        for _signature in sorted(hubs)
    ]
    components: list[ObservationComponent] = []
    for evidence_group in groups.values():
        component, code = _build_component(rows, set(evidence_group), hubs)
        if component is not None:
            components.append(component)
        elif code:
            diagnostics.append(DiscoveryDiagnostic(code, tuple(sorted(evidence_group))))
    components.sort(key=lambda item: item.support_hash)
    return DiscoveryResult(tuple(components), tuple(diagnostics))


def _strict_object(value: Any, *, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise ObservationOutputError(f"{label} has an invalid schema")
    return value


def _assertion(value: Any, allowed_claim_ids: frozenset[int]) -> ObservationAssertion:
    row = _strict_object(
        value,
        keys=frozenset({"text", "supporting_claim_ids"}),
        label="assertion",
    )
    text = str(row["text"]).strip()
    ids = row["supporting_claim_ids"]
    if not text or len(text) > 500 or not isinstance(ids, list) or not ids:
        raise ObservationOutputError("assertion is empty or oversized")
    if any(
        isinstance(item, bool)
        or not isinstance(item, int)
        or item not in allowed_claim_ids
        for item in ids
    ):
        raise ObservationOutputError("assertion cites an unknown supporting claim")
    if scan_text_for_findings(text):
        raise ObservationOutputError("assertion contains sensitive material")
    return ObservationAssertion(text, tuple(sorted(set(ids))))


def parse_synthesis_output(raw: str, *, allowed_claim_ids: Iterable[int]) -> ObservationDraft:
    """Validate provider JSON and reject any output not exactly supported."""
    if not isinstance(raw, str) or not raw.strip() or len(raw.encode("utf-8")) > 16_000:
        raise ObservationOutputError("provider output is empty or oversized")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ObservationOutputError("provider output is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("decision") not in {"emit", "no_signal"}:
        raise ObservationOutputError("provider decision is invalid")
    if payload["decision"] == "no_signal":
        _strict_object(payload, keys=frozenset({"decision"}), label="no_signal")
        return ObservationDraft(decision="no_signal")
    row = _strict_object(
        payload,
        keys=frozenset({"decision", "name", "observation_type", "summary", "assertions"}),
        label="observation",
    )
    name, summary = str(row["name"]).strip(), str(row["summary"]).strip()
    observation_type = str(row["observation_type"]).strip()
    assertions = row["assertions"]
    if not name or len(name) > 120 or not summary or len(summary) > 1200:
        raise ObservationOutputError("observation text is empty or oversized")
    if observation_type not in OBSERVATION_TYPES or not isinstance(assertions, list) or not assertions:
        raise ObservationOutputError("observation type or assertions are invalid")
    if scan_text_for_findings(f"{name}\n{summary}"):
        raise ObservationOutputError("observation contains sensitive material")
    allowed = frozenset(int(item) for item in allowed_claim_ids)
    parsed = tuple(_assertion(item, allowed) for item in assertions)
    return ObservationDraft("emit", name, observation_type, summary, parsed)
