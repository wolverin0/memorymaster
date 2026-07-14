from __future__ import annotations

import http.client
import json
import threading
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memorymaster.surfaces.dashboard import create_dashboard_server


@dataclass
class FakeCitation:
    source: str = "tests/test_dashboard_coverage.py"
    locator: str = "L1"
    excerpt: str = "dashboard coverage"


@dataclass
class FakeClaim:
    id: int
    text: str
    subject: str
    predicate: str
    object_value: str
    status: str = "confirmed"
    confidence: float = 0.8
    claim_type: str = "fact"
    scope: str = "project:memorymaster"
    source_agent: str = "pytest"
    pinned: bool = False
    citations: list[FakeCitation] = field(default_factory=lambda: [FakeCitation()])
    created_at: str = "2026-01-01T00:00:00+00:00"
    updated_at: str = "2026-01-01T00:00:00+00:00"


@dataclass
class FakeEvent:
    id: int
    claim_id: int | None
    event_type: str
    details: str = ""
    from_status: str | None = None
    to_status: str | None = None
    payload_json: str | None = None
    created_at: str = "2026-01-01T00:00:00+00:00"


@dataclass
class FakeProposal:
    id: int
    claim_id: int
    status: str = "pending"
    destination: str = "github"
    external_ref: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = "2026-01-01T00:00:00+00:00"
    updated_at: str = "2026-01-01T00:00:00+00:00"


class FakeStore:
    def __init__(self, events: list[FakeEvent]) -> None:
        self.db_path = ":memory:"
        self.events = events
        self.recorded_events: list[dict[str, Any]] = []

    def record_event(self, **kwargs: Any) -> None:
        self.recorded_events.append(kwargs)
        self.events.append(
            FakeEvent(
                id=len(self.events) + 1,
                claim_id=kwargs.get("claim_id"),
                event_type=str(kwargs.get("event_type") or "audit"),
                details=str(kwargs.get("details") or ""),
                payload_json=json.dumps(kwargs.get("payload") or {}),
            )
        )


class FakeService:
    def __init__(self) -> None:
        self.claims = [
            FakeClaim(1, "dashboard is covered", "dashboard", "coverage", "added"),
            FakeClaim(2, "dashboard item is stale", "dashboard", "status", "stale", status="stale"),
        ]
        self.events = [
            FakeEvent(1, 1, "transition", "confirmed claim", "candidate", "confirmed"),
            FakeEvent(2, 2, "audit", "triage_mark_reviewed", payload_json='{"source":"test"}'),
        ]
        self.proposals = [FakeProposal(10, claim_id=2, payload={"action": "archive"})]
        self.store = FakeStore(self.events)

    def list_claims(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        include_archived: bool = False,
    ) -> list[FakeClaim]:
        del include_archived
        rows = [claim for claim in self.claims if status is None or claim.status == status]
        return rows[:limit]

    def list_events(
        self,
        *,
        claim_id: int | None = None,
        limit: int = 100,
        event_type: str | None = None,
    ) -> list[FakeEvent]:
        rows = [
            event
            for event in self.events
            if (claim_id is None or event.claim_id == claim_id)
            and (event_type is None or event.event_type == event_type)
        ]
        return rows[:limit]

    def list_action_proposals(
        self,
        *,
        status: str | None = None,
        destination: str | None = None,
        limit: int = 100,
    ) -> list[FakeProposal]:
        rows = [
            proposal
            for proposal in self.proposals
            if (status is None or proposal.status == status)
            and (destination is None or proposal.destination == destination)
        ]
        return rows[:limit]

    def update_action_proposal_status(
        self,
        proposal_id: int,
        *,
        status: str,
        external_ref: str | None = None,
    ) -> FakeProposal:
        proposal = next(item for item in self.proposals if item.id == proposal_id)
        proposal.status = status
        proposal.external_ref = external_ref
        return proposal

    def pin(self, claim_id: int, *, pin: bool) -> FakeClaim:
        claim = next(item for item in self.claims if item.id == claim_id)
        claim.pinned = pin
        return claim


@contextmanager
def running_dashboard(tmp_path: Path, service: FakeService):
    log_path = tmp_path / "operator-events.jsonl"
    server = create_dashboard_server(
        service=service,
        workspace_root=tmp_path,
        operator_log_jsonl=log_path,
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", host, port, log_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=5) as response:
        assert response.status == 200
        return json.loads(response.read().decode("utf-8"))


def post_json(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        return json.loads(response.read().decode("utf-8"))


def test_index_route_serves_dashboard_html(tmp_path: Path) -> None:
    with running_dashboard(tmp_path, FakeService()) as (base_url, _host, _port, _log_path):
        with urllib.request.urlopen(base_url + "/", timeout=5) as response:
            body = response.read().decode("utf-8")

    assert response.status == 200
    assert "MemoryMaster Dashboard" in body
    assert "EventSource('/api/operator/stream?last=20')" in body


def test_recall_analysis_renders_as_operator_panel(tmp_path: Path) -> None:
    """P5 governance surface: recall-analysis must be operator-visible as a
    rendered dashboard panel, not a curl-only JSON endpoint. The differentiator
    of this product is governance you can SEE — an operator debugging why a
    claim ranked where it did should not need a terminal. If this panel is
    removed (regressing back to JSON-only), this test fails on intent."""
    with running_dashboard(tmp_path, FakeService()) as (base_url, _host, _port, _log_path):
        with urllib.request.urlopen(base_url + "/", timeout=5) as response:
            body = response.read().decode("utf-8")

    # The panel section, its on-demand control, its render target, and the
    # fetch wiring must all be present so the route surfaces in the UI.
    assert "Recall Analysis" in body
    assert 'id="recall-run"' in body
    assert 'id="recall-body"' in body
    assert "/api/recall-analysis?query=" in body
    assert "fillRecallAnalysis" in body


def test_recall_analysis_endpoint_returns_ranking_explainability(tmp_path: Path) -> None:
    """The panel is only useful if the backing route emits the explainability
    contract (active weights + per-claim results). This anchors on WHAT an
    operator needs to debug ranking, not on internal field plumbing."""

    class RecallService(FakeService):
        def recall_analysis(self, *, query_text: str, **_kwargs: Any) -> dict[str, Any]:
            return {
                "query": query_text,
                "mode": "hybrid",
                "profile": None,
                "rows": 1,
                "weights": {
                    "retrieval_weights": {
                        "lexical": 0.4,
                        "confidence": 0.2,
                        "freshness": 0.2,
                        "vector": 0.2,
                    }
                },
                "component_rankings": {"lexical": [1]},
                "results": [
                    {
                        "claim_id": 1,
                        "human_id": "C-1",
                        "text": "dashboard is covered",
                        "status": "confirmed",
                        "tier": "working",
                        "pinned": False,
                        "score": 0.91,
                        "lexical_score": 0.5,
                        "confidence_score": 0.2,
                        "freshness_score": 0.1,
                        "vector_score": 0.11,
                    }
                ],
            }

    with running_dashboard(tmp_path, RecallService()) as (base_url, _host, _port, _log_path):
        payload = get_json(base_url, "/api/recall-analysis?query=dashboard&mode=hybrid&limit=10")

    assert payload["ok"] is True
    # Active retrieval weights — the "why did this rank" inputs.
    assert payload["weights"]["retrieval_weights"]["lexical"] == 0.4
    # Per-claim score attribution the panel renders.
    assert payload["results"][0]["claim_id"] == 1
    assert payload["results"][0]["score"] == 0.91


def test_claim_list_route_filters_and_serializes_claim_detail(tmp_path: Path) -> None:
    with running_dashboard(tmp_path, FakeService()) as (base_url, _host, _port, _log_path):
        payload = get_json(base_url, "/api/claims?status=stale&limit=10")

    assert payload["ok"] is True
    assert payload["rows"] == 1
    assert payload["claims"][0]["id"] == 2
    assert payload["claims"][0]["subject"] == "dashboard"
    assert payload["claims"][0]["citations"][0]["source"] == "tests/test_dashboard_coverage.py"


def test_claim_event_detail_route_filters_by_claim_id(tmp_path: Path) -> None:
    with running_dashboard(tmp_path, FakeService()) as (base_url, _host, _port, _log_path):
        payload = get_json(base_url, "/api/events?claim_id=1&limit=10")

    assert payload["ok"] is True
    assert payload["rows"] == 1
    assert payload["events"][0]["claim_id"] == 1
    assert payload["events"][0]["payload"] is None


def test_review_queue_route_adds_triage_flags(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_build_review_queue(service: FakeService, **kwargs: Any) -> list[object]:
        assert service.claims[1].status == "stale"
        assert kwargs["include_stale"] is True
        assert kwargs["include_conflicted"] is False
        return [object()]

    def fake_queue_to_dicts(items: list[object]) -> list[dict[str, Any]]:
        assert len(items) == 1
        return [{"claim_id": 2, "status": "stale", "reason": "needs review", "priority": 0.9}]

    monkeypatch.setattr(
        "memorymaster.surfaces.dashboard_read_models.build_review_queue",
        fake_build_review_queue,
    )
    monkeypatch.setattr(
        "memorymaster.surfaces.dashboard_read_models.queue_to_dicts",
        fake_queue_to_dicts,
    )

    with running_dashboard(tmp_path, FakeService()) as (base_url, _host, _port, _log_path):
        payload = get_json(base_url, "/api/review-queue?include_conflicted=0&limit=5")

    assert payload["ok"] is True
    assert payload["rows"] == 1
    assert payload["items"][0]["reviewed"] is True
    assert payload["items"][0]["suppressed"] is False


def test_sse_stream_replays_tail_and_returns_without_following(tmp_path: Path) -> None:
    service = FakeService()
    with running_dashboard(tmp_path, service) as (_base_url, host, port, log_path):
        log_path.write_text(
            json.dumps({"event": "stream_start", "message": "ready"}) + "\n",
            encoding="utf-8",
        )
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/api/operator/stream?last=1&follow=0")
        response = conn.getresponse()
        event_line = response.readline().decode("utf-8")
        data_line = response.readline().decode("utf-8")
        blank_line = response.readline().decode("utf-8")
        conn.close()

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
    assert event_line == "event: stream_start\n"
    assert data_line.startswith('data: {"event": "stream_start"')
    assert blank_line == "\n"


def test_action_proposals_routes_list_and_resolve_status(tmp_path: Path) -> None:
    service = FakeService()
    with running_dashboard(tmp_path, service) as (base_url, _host, _port, _log_path):
        listed = get_json(base_url, "/api/action-proposals?status=pending&destination=github")
        resolved = post_json(
            base_url,
            "/api/action-proposals/status",
            {"proposal_id": 10, "status": "resolved", "external_ref": "pr-123"},
        )

    assert listed["rows"] == 1
    assert listed["proposals"][0]["id"] == 10
    assert resolved["ok"] is True
    assert resolved["proposal"]["status"] == "resolved"
    assert service.proposals[0].external_ref == "pr-123"


def test_triage_action_can_pin_claim(tmp_path: Path) -> None:
    service = FakeService()
    with running_dashboard(tmp_path, service) as (base_url, _host, _port, _log_path):
        payload = post_json(base_url, "/api/triage/action", {"claim_id": 1, "action": "pin"})

    assert payload["ok"] is True
    assert payload["action"] == "pin"
    assert payload["claim"]["pinned"] is True
    assert service.claims[0].pinned is True


def test_triage_action_can_approve_steward_proposal(monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake_resolve_steward_proposal(service: FakeService, **kwargs: Any) -> dict[str, Any]:
        calls.append({"service": service, **kwargs})
        return {"claim_id": kwargs["claim_id"], "applied": True}

    monkeypatch.setattr(
        "memorymaster.govern.steward.resolve_steward_proposal",
        fake_resolve_steward_proposal,
    )

    service = FakeService()
    with running_dashboard(tmp_path, service) as (base_url, _host, _port, _log_path):
        payload = post_json(
            base_url,
            "/api/triage/action",
            {"claim_id": 2, "action": "approve_proposal"},
        )

    assert payload["ok"] is True
    assert payload["action"] == "approve_proposal"
    assert payload["result"] == {"claim_id": 2, "applied": True}
    assert calls == [{"service": service, "action": "approve", "claim_id": 2, "apply_on_approve": True}]
