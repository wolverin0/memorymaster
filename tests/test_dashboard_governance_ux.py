"""R4.3 governance-state and accessibility acceptance contracts."""

from __future__ import annotations

from io import BytesIO
from html.parser import HTMLParser
import json

import pytest
from memorymaster.surfaces.dashboard import DashboardRequestHandler
from memorymaster.surfaces import dashboard_commands, dashboard_read_models


def _dashboard_html() -> str:
    handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
    handler.wfile = BytesIO()
    handler.send_response = lambda *_args, **_kwargs: None
    handler.send_header = lambda *_args, **_kwargs: None
    handler.end_headers = lambda *_args, **_kwargs: None
    handler._write_dashboard()
    return handler.wfile.getvalue().decode("utf-8")


class _ControlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.unlabelled: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"input", "select"}:
            return
        values = dict(attrs)
        if not values.get("aria-label") and not values.get("aria-labelledby"):
            self.unlabelled.append(values.get("id") or tag)


def test_review_mutation_waits_for_success_and_exposes_retry_state() -> None:
    html = _dashboard_html()
    assert "r.remove();await jpost" not in html
    assert "data-pending" in html
    assert "Action failed" in html
    assert "Retry" in html
    assert 'id="review-status"' in html
    assert 'aria-live="polite"' in html


def test_panel_failures_are_not_silently_rendered_as_empty() -> None:
    html = _dashboard_html()
    assert ".catch(()=>{})" not in html
    assert "showPanelFailure" in html
    assert "Could not load" in html


def test_controls_live_regions_and_mobile_layout_are_accessible() -> None:
    html = _dashboard_html()
    parser = _ControlParser()
    parser.feed(html)
    assert parser.unlabelled == []
    assert '@media(max-width:800px)' in html
    assert "grid-template-columns:1fr" in html
    assert 'role="status"' in html


def test_archived_and_stopped_text_meet_the_normal_text_contrast_token() -> None:
    html = _dashboard_html()
    assert ".badge-archived{background:#1e293b;color:#94a3b8" in html
    assert 'span[style="color:#64748b"]{color:#94a3b8!important}' in html


def test_governance_evidence_and_consequences_are_directly_inspectable() -> None:
    html = _dashboard_html()
    assert "Evidence &amp; lineage" in html
    assert "/lineage" in html
    assert "Citations" in html
    assert "Action consequences" in html
    assert 'class="conflict-compare"' in html
    assert "proposalEvidence(p)" in html
    assert "const proposalActions=proposalId>0?" in html


def test_review_queue_exposes_only_the_latest_pending_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard_read_models, "build_review_queue", lambda *_args, **_kwargs: [object()])
    monkeypatch.setattr(
        dashboard_read_models,
        "queue_to_dicts",
        lambda _items: [{"claim_id": 7, "status": "stale", "reason": "age", "priority": 0.9}],
    )
    monkeypatch.setattr(dashboard_read_models, "triage_flags", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        dashboard_read_models,
        "list_steward_proposals",
        lambda *_args, **_kwargs: [
            {"proposal_event_id": 3, "claim_id": 7, "proposal_decision": "stale_candidate"},
            {
                "proposal_event_id": 9,
                "claim_id": 7,
                "proposal_decision": "archive_candidate",
                "proposed_status": "archived",
                "reasons": [{"code": "age", "message": "Retention elapsed"}],
            },
        ],
    )

    payload = dashboard_read_models.review_queue_payload(
        object(),
        limit=30,
        include_stale=True,
        include_conflicted=True,
        allow_sensitive=False,
        exclude_reviewed=True,
        exclude_suppressed=True,
    )

    assert payload["items"][0]["proposal"]["proposal_event_id"] == 9
    assert payload["items"][0]["proposal"]["reasons"][0]["message"] == "Retention elapsed"


def test_proposal_action_resolves_the_exact_displayed_event(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_resolve(_service: object, **kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"resolved": True, "proposal_event_id": kwargs.get("proposal_event_id")}

    monkeypatch.setattr("memorymaster.govern.steward.resolve_steward_proposal", fake_resolve)
    result = dashboard_commands.apply_triage_action(
        object(),
        {"claim_id": 7, "proposal_event_id": 9, "action": "approve_proposal"},
        serialize_claim=lambda claim: claim,
    )

    assert result["result"]["proposal_event_id"] == 9
    assert calls == [{"action": "approve", "proposal_event_id": 9, "apply_on_approve": True}]
    with pytest.raises(ValueError, match="proposal_event_id must be positive"):
        dashboard_commands.apply_triage_action(
            object(),
            {"claim_id": 7, "action": "approve_proposal"},
            serialize_claim=lambda claim: claim,
        )
    for malformed_id in (True, 9.7, "9"):
        with pytest.raises(ValueError, match="proposal_event_id must be positive"):
            dashboard_commands.apply_triage_action(
                object(),
                {"claim_id": 7, "proposal_event_id": malformed_id, "action": "approve_proposal"},
                serialize_claim=lambda claim: claim,
            )


def test_browser_preserves_failed_review_action_and_distinguishes_load_error() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    html = _dashboard_html()
    state = {"action_calls": 0, "resolved": False, "proposal_event_id": None}
    conflict = {
        "id": 4,
        "status": "conflicted",
        "subject": "release",
        "predicate": "ready",
        "object_value": "no",
        "confidence": 0.6,
        "citations": [{"source": "test", "locator": "case", "excerpt": "evidence"}],
    }
    review_item = {
        "claim_id": 7,
        "status": "stale",
        "reason": "requires governance review",
        "priority": 0.9,
        "proposal": {
            "proposal_event_id": 9,
            "proposal_decision": "archive_candidate",
            "proposed_status": "archived",
            "reasons": [{"code": "age", "message": "Retention elapsed"}],
        },
    }

    def route_request(route: object) -> None:
        request = route.request
        path = request.url.split("dashboard.test", 1)[-1]
        if path == "/":
            route.fulfill(status=200, content_type="text/html", body=html)
            return
        if path.startswith("/api/claims"):
            route.fulfill(status=503, content_type="application/json", body='{"ok":false,"error":"offline"}')
            return
        if path.startswith("/api/review-queue"):
            items = [] if state["resolved"] else [review_item]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "items": items}))
            return
        if path.startswith("/api/conflicts"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "groups": [{"subject": "release", "predicate": "ready", "scope": "project", "claims": [conflict, {**conflict, "id": 2, "object_value": "yes"}]}]}),
            )
            return
        if path.startswith("/api/triage/action"):
            state["action_calls"] += 1
            state["proposal_event_id"] = request.post_data_json.get("proposal_event_id")
            if state["action_calls"] == 1:
                route.fulfill(status=409, content_type="application/json", body='{"ok":false,"error":"rejected"}')
            else:
                state["resolved"] = True
                route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
            return
        route.fulfill(status=200, content_type="application/json", body='{"ok":true}')

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 375, "height": 812})
        page.route("**/*", route_request)
        page.goto("http://dashboard.test/")
        page.get_by_role("button", name="Approve proposal for claim 7").click()
        page.get_by_text("Action failed for claim 7").wait_for()
        assert state["proposal_event_id"] == 9
        assert page.locator('tr[data-claim-id="7"]').count() == 1
        assert page.get_by_role("button", name="Approve proposal for claim 7").text_content() == "Retry"
        assert "Could not load claims" in page.locator("#claims-body").inner_text()
        assert page.locator(".conflict-compare").evaluate("node => getComputedStyle(node).gridTemplateColumns").count(" ") == 0
        page.get_by_role("button", name="Approve proposal for claim 7").click()
        page.locator('tr[data-claim-id="7"]').wait_for(state="detached")
        browser.close()
