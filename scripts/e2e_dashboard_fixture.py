"""Disposable browser fixture only. Never opens the installed database."""

import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memorymaster.surfaces.dashboard import DashboardRequestHandler, create_dashboard_server  # noqa: E402
from memorymaster.dreaming.ledger import DreamLedger  # noqa: E402
from memorymaster.operations.review_attempt import atomic_json  # noqa: E402


def seed_state(state):
    metadata = json.loads(Path("artifacts/e2e-fixes-20260907/browser-fixture.json").read_text(encoding="utf-8"))
    root = Path(metadata["root"]).resolve()
    if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith("mm-e2e-browser-"):
        raise ValueError("Only this disposable fixture can be seeded")
    if state == "error":
        (root / "ledger.db").write_bytes(b"broken fixture")
        return
    ledger = DreamLedger(root / "ledger.db")
    helpers = runpy.run_path("tests/test_dreaming_worker.py")
    capture = helpers["_capture"](ledger)
    ledger.set_extraction(capture, [], "fixture")
    ledger.defer_consolidation(capture, "fixture")
    if state == "healthy":
        ledger.mark_applied(capture, "fixture")
    now = datetime.now(timezone.utc)
    when = now - timedelta(hours=2) if state == "expired" else now
    attempt = {"attempt_id": "fixture", "started_at": when.isoformat(),
               "deadline_at": (when+timedelta(minutes=24)).isoformat(), "interval_seconds": 3600,
               "completed_at": None if state == "pending" else when.isoformat(), "outcome": "PASS"}
    atomic_json(root / "review" / "attempt.json", attempt)
    atomic_json(root / "review" / "latest.json", {"attempt_id": "fixture", "verdict": "PASS"})


def main():
    root = Path(tempfile.mkdtemp(prefix="mm-e2e-browser-"))
    os.environ["MEMORYMASTER_DASHBOARD_LEGACY_LOCAL"] = "1"
    os.environ["MEMORYMASTER_CAPTURE_STATE_DB"] = str(root / "ledger.db")
    os.environ["MEMORYMASTER_REVIEW_RESULTS"] = str(root / "review")
    helpers = runpy.run_path("tests/test_capture_inbox_dashboard.py")
    service, db, workspace, receipt, claim = helpers["_fixture"](root)
    service.store.apply_status_transition(claim, to_status="candidate", reason="browser fixture", event_type="transition")
    server = create_dashboard_server(service=service, db_target=db, workspace_root=workspace, port=0)
    server.RequestHandlerClass = DashboardRequestHandler
    metadata = {"url": f"http://127.0.0.1:{server.server_address[1]}", "db": str(db),
                "root": str(root), "claim_id": claim.id, "source_id": receipt.source_item["id"]}
    destination = Path("artifacts/e2e-fixes-20260907/browser-fixture.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(metadata), encoding="utf-8")
    print(metadata["url"], flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    seed_state(sys.argv[1]) if len(sys.argv) > 1 else main()
