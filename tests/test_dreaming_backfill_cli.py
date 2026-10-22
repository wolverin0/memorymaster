"""Disposable campaign journeys and read-only operator previews."""

import gc
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from memorymaster.core.intake_policy import reset_intake_state
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.dreaming.backfill import BackfillConfig, BackfillRunner
from memorymaster.dreaming.latency_guard import GuardDecision
from memorymaster.dreaming.models import (
    ConsolidationResult, DreamCandidate, DreamDecision, ExtractionResult, ProviderUsage,
)
from test_dreaming_worker import _accepted_review


NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(tmp_path / "fixture-aliases.json"))
    monkeypatch.setattr(MemoryService, "_init_qdrant", staticmethod(lambda: None))
    monkeypatch.setattr("memorymaster.core.webhook.fire_webhook", lambda *args, **kwargs: None)
    monkeypatch.setattr("memorymaster.knowledge.entity_ingest.resolve_claim_entities", lambda *args, **kwargs: 0)
    reset_intake_state()


def _usage(provider, model):
    return ProviderUsage(provider, model, 200, 1, 100, 20, True)


class FixtureExtractor:
    provider, model = "fixture", "fixture/extractor"

    def __init__(self, count=1):
        self.count, self.calls = count, []

    def extract(self, messages, *, scope, capture_hash):
        self.calls.append((scope, capture_hash, messages))
        source = messages[0]
        candidates = tuple(DreamCandidate(
            f"{capture_hash[:16]}-{index}", f"{source['text']} Rule {index}.", "decision",
            "project", "preserves", f"architecture-{capture_hash[:12].translate(str.maketrans('0123456789', 'ghijklmnop'))}-{index}", "project",
            source["id"] if "id" in source else source["message_id"], source["text"], .9,
        ) for index in range(self.count))
        return ExtractionResult(candidates, _usage(self.provider, self.model))


class FixtureConsolidator:
    provider, model = "fixture", "fixture/sonnet-consolidator"

    def __init__(self):
        self.calls = []

    def consolidate(self, candidates, current_claims, *, scope):
        self.calls.append((candidates, current_claims, scope))
        return ConsolidationResult(tuple(DreamDecision(
            candidate.candidate_id, "add", "synthetic reviewed selection", .9,
            source_review=_accepted_review(candidate, scope),
        ) for candidate in candidates), _usage(self.provider, self.model))


class FixtureJudge:
    provider = "fixture"

    def __init__(self, model):
        self.model, self.calls = model, []

    def judge(self, item, *, audit):
        self.calls.append((item, audit))
        return {"junk": False, "contradiction": False, "missed_useful": False,
                "secret": False, "cross_scope": False, "self_observation": False}


def _healthy(stage):
    return GuardDecision("allow", [], {"dense": {"n": 5, "p95_ms": 20}, "hook": {"n": 5, "p95_ms": 30}})


def _write_source(path, repo, session, *, provider="codex", turns=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    if provider == "codex":
        entries.append({"type": "session_meta", "payload": {"id": session, "cwd": str(repo)}, "timestamp": NOW.isoformat()})
    for index in range(turns):
        text = f"We decided project {session} must preserve cited architecture decisions {index}."
        if turns > 1:
            text += " Authored design context." * 80
        if provider == "codex":
            entry = {"type": "response_item", "timestamp": NOW.isoformat(), "payload": {
                "type": "message", "role": "user", "id": f"{path.stem}-{index}",
                "content": [{"type": "input_text", "text": text}]}}
        else:
            entry = {"type": "user", "sessionId": session, "cwd": str(repo), "uuid": f"{path.stem}-{index}",
                     "timestamp": NOW.isoformat(), "message": {"role": "user", "content": text}}
        entries.append(entry)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")


def _campaign(tmp_path, *, sessions=1, candidates=1, provider="codex", child=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    sources = tmp_path / "sources"
    for index in range(sessions):
        _write_source(sources / f"session-{index}.jsonl", repo, f"session-{index}", provider=provider)
    if child:
        _write_source(sources / "session-0.jsonl", repo, "session-0", provider=provider, turns=50)
        _write_source(sources / "session-0" / "subagents" / "child.jsonl", repo, "session-0", provider=provider, turns=50)
    db = tmp_path / "claims.db"
    writable = MemoryService(db, workspace_root=repo)
    writable.init_db()
    factories, sleeps = [], []

    def reference_service():
        factories.append("reference")
        return MemoryService(db, workspace_root=repo, read_only=True)

    def application_service():
        factories.append("application")
        return MemoryService(db, workspace_root=repo)

    extractor, consolidator = FixtureExtractor(candidates), FixtureConsolidator()
    haiku, sonnet = FixtureJudge("fixture/haiku"), FixtureJudge("fixture/sonnet")
    runner = BackfillRunner(
        BackfillConfig(tmp_path / "state", {provider: sources}, campaign_id="fixture"),
        extractor=extractor, consolidator=consolidator, reference_service_factory=reference_service,
        application_service_factory=application_service, haiku=haiku, sonnet=sonnet,
        guard=_healthy, now=lambda: NOW, sleep=sleeps.append,
    )
    return SimpleNamespace(runner=runner, db=db, service=writable, sources=sources, repo=repo,
                           factories=factories, extractor=extractor, consolidator=consolidator,
                           haiku=haiku, sonnet=sonnet, sleeps=sleeps)


def _claims(harness):
    return harness.service.list_claims(status="candidate", requesting_agent="dream-worker", limit=100)


def _files(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("sessions,repairs", [(3, 1), (11, 2)])
def test_cohort_repair_allowance_rounds_up_after_planning_all_sessions(tmp_path, sessions, repairs):
    fixture = _campaign(tmp_path, sessions=sessions)
    runner = fixture.runner
    extract = fixture.extractor.extract
    calls = 0

    def transient(messages, **kwargs):
        nonlocal calls
        calls += 1
        planned = [window for row in runner._rows()
                   for window in row["metadata"].get("selection", {}).get("windows", [])]
        assert len(planned) == sessions
        if calls <= repairs:
            raise RuntimeError("fixture transient extraction failure")
        return extract(messages, **kwargs)

    fixture.extractor.extract = transient
    runner.inventory()
    for _ in range(repairs):
        with pytest.raises(RuntimeError, match="fixture transient extraction failure"):
            runner.run(stage="extract")

    runner.run(stage="extract")

    assert calls == sessions + repairs
    assert runner._cohort_complete(runner._rows())
    assert all(latch["state"] == "running" for latch in runner._latches().values())


def test_three_window_cohort_stops_before_a_second_repair_dispatch(tmp_path):
    fixture = _campaign(tmp_path, sessions=3)
    runner = fixture.runner
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(kwargs["capture_hash"])
        raise RuntimeError("fixture transient extraction failure")

    fixture.extractor.extract = unavailable
    runner.inventory()
    for _ in range(2):
        with pytest.raises(RuntimeError, match="fixture transient extraction failure"):
            runner.run(stage="extract")
    with pytest.raises(RuntimeError, match="extraction_repair_allowance_exhausted"):
        runner.run(stage="extract")
    assert len(calls) == 2


def test_dry_inventory_constructs_no_ledger_service_or_native_database(tmp_path, monkeypatch):
    import json
    import memorymaster.dreaming.backfill as module

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "one.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {"id": "one", "cwd": str(repo)},
                                                   "timestamp": "2026-10-01T00:00:00Z"}) + "\n")
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run attempted a database")
    monkeypatch.setattr(module, "DreamLedger", forbidden)
    state = tmp_path / "state"
    runner = BackfillRunner(BackfillConfig(state, {"codex": sources}))
    report = runner.inventory(dry_run=True)
    assert report["files"] == 1 and report["unresolved"] == 0
    assert str(repo) not in json.dumps(report)
    assert not state.exists()


def test_inventory_preserves_live_scan_and_appends_for_later_campaign(tmp_path):
    import json

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    source = tmp_path / "sources"
    source.mkdir()
    path = source / "s.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "one", "cwd": str(repo)}}) + "\n")
    runner = BackfillRunner(BackfillConfig(tmp_path / "state", {"codex": source}))
    runner.ledger.set_scan_watermark("scan:codex", 123)
    runner.inventory()
    row = runner._rows()[0]
    with path.open("a") as handle:
        handle.write("{}\n")
    runner.inventory()
    assert runner._rows()[0]["frozen_end"] == row["frozen_end"]
    assert runner.ledger.scan_watermark("scan:codex") == 123
    assert runner._worker(None).config.idle_minutes == 0


def test_fixture_campaign_requires_explicit_apply_and_replays_idempotently(tmp_path):
    fixture = _campaign(tmp_path)
    runner = fixture.runner
    runner.inventory()
    runner.run(stage="extract")
    assert len(fixture.extractor.calls) == 1
    assert fixture.factories == []
    assert not fixture.consolidator.calls and not fixture.haiku.calls and not fixture.sonnet.calls
    assert _claims(fixture) == []

    runner.run(stage="consolidate")
    assert fixture.factories == ["reference"]
    runner.run(stage="quality")
    receipt = runner._rows()[0]["metadata"]["quality_receipt"]
    assert receipt["passed"] and all(child["complete"] for child in receipt["strata"].values())
    assert fixture.haiku.calls and fixture.sonnet.calls
    runner.run(stage="apply", apply_candidates=False)
    assert "application" not in fixture.factories and _claims(fixture) == []

    runner.run(stage="apply", apply_candidates=True)
    claims = _claims(fixture)
    assert len(claims) == 1
    claim = claims[0]
    assert claim.source_agent == "dream-worker" and claim.status == "candidate"
    assert claim.citations[0].source == "dream-worker"
    assert claim.citations[0].locator.startswith("dream:codex:")
    row = runner._rows()[0]
    application = next(iter(row["metadata"]["applications"].values()))
    assert application["completed"] and application["created_claim_ids"] == [claim.id]
    assert application["reused_claim_ids"] == []
    assert application["intake_batch_id"].startswith("ao7-fixture-")
    first_events = fixture.service.store.list_events(claim.id)
    runner.run(stage="apply", apply_candidates=True)
    assert [item.id for item in _claims(fixture)] == [claim.id]
    assert fixture.service.store.list_events(claim.id) == first_events
    assert len(fixture.extractor.calls) == len(fixture.consolidator.calls) == 1


def test_partial_paused_extraction_cannot_release_or_write_from_all_stage(tmp_path):
    fixture = _campaign(tmp_path, sessions=2)
    runner = fixture.runner
    runner.inventory()

    def pause_after_first_extract(stage):
        if stage == "extract" and fixture.extractor.calls:
            return GuardDecision("pause", ["fixture_latency_pause"], {})
        return _healthy(stage)

    runner.guard = pause_after_first_extract
    try:
        runner.run(stage="all", apply_candidates=True)
    except ValueError as exc:
        assert any(word in str(exc).lower() for word in ("incomplete", "extract", "pause"))
    assert len(fixture.extractor.calls) == 1
    assert fixture.factories == []
    assert not fixture.haiku.calls and not fixture.sonnet.calls
    assert not fixture.consolidator.calls
    assert all("quality_receipt" not in row["metadata"] for row in runner._rows())
    assert _claims(fixture) == []


@pytest.mark.parametrize("change", ["source", "reference", "judge"])
def test_changed_release_identity_refuses_candidate_application(tmp_path, change):
    fixture = _campaign(tmp_path)
    runner = fixture.runner
    runner.inventory()
    runner.run(stage="all", apply_candidates=False)
    assert runner._rows()[0]["metadata"]["quality_receipt"]["passed"]
    if change == "source":
        path = fixture.sources / "session-0.jsonl"
        path.write_bytes(path.read_bytes().replace(b"cited", b"plain"))
    elif change == "reference":
        fixture.service.ingest(
            "The project now requires independent audit evidence.", [CitationInput("independent")],
            scope=runner._rows()[0]["scope"], source_agent="independent-writer",
        )
    else:
        fixture.sonnet.model = "fixture/sonnet-changed"
    with pytest.raises(ValueError):
        runner.run(stage="apply", apply_candidates=True)
    assert not any(claim.source_agent == "dream-worker" for claim in _claims(fixture))
    assert all(not row["metadata"].get("applications") for row in runner._rows())


@pytest.mark.parametrize("action", ["inventory", "run", "pause", "resume", "status", "rollback"])
def test_cli_dry_actions_construct_no_services_or_providers_and_write_no_bytes(tmp_path, monkeypatch, capsys, action):
    import memorymaster.core.service as service_module
    import memorymaster.dreaming.backfill as backfill_module
    import memorymaster.dreaming.providers as providers
    from memorymaster.surfaces import cli

    fixture = _campaign(tmp_path)
    fixture.runner.inventory()
    # sqlite connection context managers commit without closing. Collect the
    # fixture setup connections so delayed WAL checkpointing is not blamed on CLI.
    gc.collect()
    # Freeze all expected campaign bytes before invoking the actual CLI surface.
    before = _files(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("dry CLI action constructed a ledger, service or provider")

    monkeypatch.setattr(backfill_module, "DreamLedger", forbidden)
    monkeypatch.setattr(service_module, "MemoryService", forbidden)
    monkeypatch.setattr(cli, "MemoryService", forbidden)
    monkeypatch.setattr(providers, "OpenCodeExtractor", forbidden)
    monkeypatch.setattr(providers, "OpenCodeConsolidator", forbidden)
    monkeypatch.setattr(cli, "_resolve_db_path", lambda args: str(fixture.db))
    code = cli.main([
        "--json", "dream-backfill", action, "--dry-run", "--state-dir", str(fixture.runner.config.state_dir),
        "--campaign", "fixture", "--cohort", "codex-0", "--source-root", f"codex={fixture.sources}",
    ])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert isinstance(report, dict)
    assert _files(tmp_path) == before


def test_parent_precedes_later_subagent_in_preview_and_run(tmp_path, monkeypatch):
    import memorymaster.dreaming.backfill as module

    fixture = _campaign(tmp_path, provider="claude", child=True)
    child = fixture.sources / "session-0" / "subagents" / "child.jsonl"
    child.write_text(child.read_text(encoding="utf-8").replace(NOW.isoformat(), NOW.replace(day=21).isoformat()), encoding="utf-8")
    select = module.select_windows
    seen = []

    def recording_selector(turns, *, scope):
        turns = list(turns)
        seen.append([turn.stream_id for turn in turns])
        return select(turns, scope=scope)

    monkeypatch.setattr(module, "select_windows", recording_selector)
    preview = fixture.runner.inventory(dry_run=True)
    fixture.runner.inventory()
    fixture.runner.run(stage="extract")

    rows = fixture.runner._rows()
    parent = next(row for row in rows if row["metadata"]["source_family"] == "claude")
    assert len(seen) == 2
    assert seen[0] == seen[1]
    assert seen[0][0] == parent["stream_id"]
    assert parent["metadata"]["selection_owner"] == parent["stream_id"]
    assert preview["offline_selection"]["windows"] == len(parent["metadata"]["selection"]["windows"])


def test_parent_and_child_share_one_session_and_four_window_allowance(tmp_path):
    fixture = _campaign(tmp_path, provider="claude", child=True)
    report = fixture.runner.inventory()
    assert report["files"] == 2
    assert report["sessions_by_source"] == {"claude": 1}
    assert report["max_extraction_calls"] == 4
    fixture.runner.run(stage="extract")
    assert fixture.factories == []
    rows = fixture.runner._rows()
    assert len({row["parent_session_id"] for row in rows}) == 1
    selections = [row["metadata"]["selection"] for row in rows if "selection" in row["metadata"]]
    assert len(selections) == 1
    windows = selections[0]["windows"]
    assert 1 <= len(windows) == len(fixture.extractor.calls) <= 4
    assert {item["stream"] for window in windows for item in window["ranges"]} == {row["stream_id"] for row in rows}


def test_manual_guard_latch_needs_fresh_evidence_before_extract_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr("memorymaster.dreaming.backfill_guard.time.time", lambda: NOW.timestamp())
    fixture = _campaign(tmp_path)
    runner = fixture.runner
    runner.inventory()
    runner.pause(state="quality_paused")
    runner.run(stage="extract")
    assert fixture.extractor.calls == []
    fresh = {"diagnosis": "fixture diagnosed", "disposition": "cohort reviewed", "passed": True,
             "checked_at": NOW.timestamp()}
    before = _files(runner.config.state_dir)
    for evidence in ({}, {**fresh, "checked_at": fresh["checked_at"] - 120}):
        with pytest.raises(ValueError):
            runner.resume(evidence=evidence)
        assert _files(runner.config.state_dir) == before
    runner.resume(evidence=fresh)
    runner.run(stage="extract")
    assert len(fixture.extractor.calls) == 1
    assert fixture.factories == []


def test_candidate_application_paces_each_twenty_five_operations(tmp_path):
    fixture = _campaign(tmp_path, sessions=6, candidates=5)
    fixture.runner.inventory()
    fixture.runner.run(stage="all", apply_candidates=True)
    assert len(_claims(fixture)) == 30
    assert fixture.sleeps == [.25]


def test_unenforceable_runtime_fails_before_creating_campaign_state(tmp_path, capsys):
    from memorymaster.surfaces.cli import main

    state = tmp_path / "state"
    assert main(["dream-backfill", "run", "--state-dir", str(state),
                 "--extractor-model", "anthropic/claude-haiku-4-5"]) == 2
    assert "cannot enforce" in capsys.readouterr().out
    assert not state.exists()


@pytest.mark.parametrize("stage", ["consolidate", "quality", "apply", "all"])
def test_gemini_api_non_extract_cli_fails_before_binding_runtime(tmp_path, monkeypatch, capsys, stage):
    from memorymaster.surfaces.cli import main

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported stage constructed a provider or service")

    monkeypatch.setattr("memorymaster.dreaming.providers.GeminiBoundedClient", forbidden)
    monkeypatch.setattr("memorymaster.core.service.MemoryService", forbidden)
    state = tmp_path / "state"
    database = tmp_path / "never-open.db"
    assert main([
        "dream-backfill", "run", "--state-dir", str(state), "--client", "gemini-api",
        "--extractor-model", "gemini-api/gemini-3.5-flash-lite", "--stage", stage,
        "--consolidator-model", "anthropic/claude-sonnet-4-6", "--memory-db", str(database),
        "--probe-scope", "project:test", "--haiku-model", "anthropic/claude-haiku-4-5",
        "--sonnet-model", "anthropic/claude-sonnet-4-6",
    ]) == 2
    assert "--client gemini-api supports only --stage extract" in capsys.readouterr().out
    assert not state.exists()
    assert not database.exists()


def test_gemini_api_cli_preflight_binds_extractor_with_fake_factory(tmp_path, monkeypatch):
    from memorymaster.surfaces.backfill_cli import configure_backfill_runtime
    from memorymaster.surfaces.cli import build_parser

    args = build_parser().parse_args([
        "dream-backfill", "run", "--state-dir", str(tmp_path / "state"),
        "--client", "gemini-api", "--extractor-model", "gemini-api/gemini-3.5-flash-lite",
    ])
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(complete_bounded=lambda *_args, **_kwargs: pytest.fail("preflight dispatched"))

    monkeypatch.setattr("memorymaster.dreaming.providers.GeminiBoundedClient", factory)
    runner = BackfillRunner(BackfillConfig(args.state_dir, {}, args.campaign))
    configure_backfill_runtime(args, runner)
    assert runner.extractor.model == args.extractor_model
    assert callable(runner.extractor.client.complete_bounded)
    assert calls == [{"model": args.extractor_model, "work_dir": args.state_dir / "provider-slot-ao7"}]
    assert not args.state_dir.exists()


@pytest.mark.parametrize("stage", ["extract", "consolidate", "quality", "apply", "all"])
def test_gemini_api_cli_preflight_uses_real_client_and_stage_validation(tmp_path, monkeypatch, stage):
    from memorymaster.dreaming.providers import GeminiBoundedClient
    from memorymaster.surfaces.backfill_cli import configure_backfill_runtime
    from memorymaster.surfaces.cli import build_parser

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def forbidden(*args, **kwargs):
        pytest.fail("preflight dispatched a provider request or constructed a service")

    monkeypatch.setattr("memorymaster.dreaming.providers._post_with_retry", forbidden)
    monkeypatch.setattr("memorymaster.core.service.MemoryService", forbidden)
    args = build_parser().parse_args([
        "dream-backfill", "run", "--state-dir", str(tmp_path / "state"),
        "--client", "gemini-api", "--extractor-model", "gemini-api/gemini-3.5-flash-lite",
        "--stage", stage, "--consolidator-model", "anthropic/claude-sonnet-4-6",
        "--memory-db", str(tmp_path / "never-open.db"), "--probe-scope", "project:test",
        "--haiku-model", "anthropic/claude-haiku-4-5", "--sonnet-model", "anthropic/claude-sonnet-4-6",
    ])

    runner = BackfillRunner(BackfillConfig(args.state_dir, {}, args.campaign))
    if stage == "extract":
        configure_backfill_runtime(args, runner)
        assert isinstance(runner.extractor.client, GeminiBoundedClient)
        assert runner.extractor.client.model == args.extractor_model
    else:
        with pytest.raises(ValueError, match="--client gemini-api supports only --stage extract"):
            configure_backfill_runtime(args, runner)
    assert not args.state_dir.exists()
    assert not args.memory_db.exists()


def test_gemini_api_cli_preflight_rejects_non_gemini_extractor_with_real_client(tmp_path):
    from memorymaster.dreaming.providers import ProviderConfigError
    from memorymaster.surfaces.backfill_cli import configure_backfill_runtime
    from memorymaster.surfaces.cli import build_parser

    args = build_parser().parse_args([
        "dream-backfill", "run", "--state-dir", str(tmp_path / "state"),
        "--client", "gemini-api", "--extractor-model", "anthropic/claude-haiku-4-5",
    ])
    runner = BackfillRunner(BackfillConfig(args.state_dir, {}, args.campaign))
    with pytest.raises(ProviderConfigError) as caught:
        configure_backfill_runtime(args, runner)
    assert caught.value.reason == "invalid_model"
    assert not args.state_dir.exists()


@pytest.mark.parametrize("client_args", [[], ["--client", "opencode"]])
def test_opencode_cli_preflight_still_rejects_unbounded_client(tmp_path, client_args):
    from memorymaster.surfaces.backfill_cli import configure_backfill_runtime
    from memorymaster.surfaces.cli import build_parser

    args = build_parser().parse_args([
        "dream-backfill", "run", "--state-dir", str(tmp_path / "state"),
        "--extractor-model", "anthropic/claude-haiku-4-5", *client_args,
    ])
    assert args.client == "opencode"
    runner = BackfillRunner(BackfillConfig(args.state_dir, {}, args.campaign))
    with pytest.raises(ValueError, match="configured route cannot enforce provider output limits"):
        configure_backfill_runtime(args, runner)
    assert not args.state_dir.exists()


def test_child_quality_is_reported_separately_and_privacy_latches_campaign(tmp_path):
    fixture = _campaign(tmp_path, provider="claude", child=True)
    fixture.runner.inventory()
    fixture.runner.run(stage="all")
    rows = fixture.runner._rows()
    receipt = next(row["metadata"]["quality_receipt"] for row in rows if "quality_receipt" in row["metadata"])
    assert set(receipt["strata"]) == {"claude", "claude_subagent"}
    assert receipt["complete"] and receipt["passed"]
    # A source leak uses a campaign latch, and cannot be cleared by another run.
    for key, _ in fixture.haiku.calls:
        assert "source_context" in key or "messages" in key

    class Leak(FixtureJudge):
        def judge(self, item, *, audit):
            return {**super().judge(item, audit=audit), "secret": True}

    fixture.runner.haiku = Leak("fixture/leak-haiku")
    fixture.runner.run(stage="quality")
    assert fixture.runner._latches()["fixture:campaign"]["state"] == "quality_paused"
    fixture.runner.run(stage="apply", apply_candidates=True)
    assert _claims(fixture) == []


def test_isolated_latency_stage_pause_does_not_pause_stateless_extraction(tmp_path):
    fixture = _campaign(tmp_path)
    fixture.runner.inventory()
    fixture.runner.guard = lambda stage: GuardDecision("allow", ["insufficient_data"])
    assert not fixture.runner._allowed("codex-0", "consolidate")
    assert fixture.runner._allowed("codex-0", "extract")
    assert fixture.runner._latches()["fixture:codex-0:consolidate"]["state"] == "latency_paused"


def test_daily_attempt_reservation_defers_without_dispatch_and_cohort_budget_latches(tmp_path):
    from dataclasses import replace

    fixture = _campaign(tmp_path)
    runner = fixture.runner
    runner.config = replace(runner.config, max_extract_daily=1, max_consolidate_cohort=2)
    runner.inventory()
    first = runner._reserve("codex-0", "extract", 10_000, 1500)
    with pytest.raises(RuntimeError, match="daily_budget_deferred"):
        runner._reserve("codex-0", "extract", 1, 1)
    assert first and not fixture.extractor.calls
    runner._reserve("codex-0", "consolidate", 30_000, 4000)
    runner.now = lambda: NOW.replace(day=21)
    runner._reserve("codex-0", "consolidate", 30_000, 4000)
    runner.now = lambda: NOW.replace(day=22)
    with pytest.raises(RuntimeError, match="cohort_budget_paused"):
        runner._reserve("codex-0", "consolidate", 1, 1)
    assert runner._latches()["fixture:codex-0"]["state"] == "budget_paused"
