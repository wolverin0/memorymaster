"""Default-off AO-7 guards, exercised only against disposable campaigns."""

import json
from dataclasses import replace

import pytest

from memorymaster.dreaming.backfill import read_backfill_status
from test_dreaming_backfill_cli import _campaign, isolated_configuration as isolated_configuration


def _extracted(tmp_path):
    fixture = _campaign(tmp_path)
    fixture.runner.inventory()
    fixture.runner.run(stage="extract")
    capture = fixture.runner._captures(fixture.runner._rows())[0]
    return fixture, capture


def _candidate(fixture, capture, **changes):
    candidate = {**capture["extraction"][0], **changes}
    fixture.runner.ledger.set_extraction(capture["id"], [candidate], "fixture")
    # Keep the fixture's cited message exact without rewriting frozen transcripts.
    original = fixture.runner._captures

    def captures(rows):
        result = original(rows)
        for item in result:
            item["messages"][0]["text"] = candidate["evidence_quote"]
        return result

    fixture.runner._captures = captures
    return candidate


@pytest.mark.parametrize("signal", ["subject", "quote", "worktree"])
def test_content_scope_uses_project_alias(tmp_path, monkeypatch, signal):
    monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", "1")
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"aliases": {"bridge-checkout": "project:wezbridge"}}))
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(aliases))
    fixture, capture = _extracted(tmp_path)
    changes = {"subject": "bridge-checkout"} if signal == "subject" else {
        "evidence_quote": "Use bridge-checkout for relay delivery."}
    if signal == "worktree":
        repo = tmp_path / "bridge-checkout"
        (repo / ".git" / "worktrees" / "relay").mkdir(parents=True)
        worktree = tmp_path / "_wt" / "relay"
        worktree.mkdir(parents=True)
        gitdir = repo / ".git" / "worktrees" / "relay"
        (worktree / ".git").write_text(f"gitdir: {gitdir.as_posix()}")
        (gitdir / "commondir").write_text("../..")
        (gitdir / "gitdir").write_text(str(worktree / ".git"))
        changes = {"evidence_quote": f'Use the relay checkout "{worktree.as_posix()}".'}
        original = fixture.consolidator.consolidate

        def privacy_review(*args, **kwargs):
            result = original(*args, **kwargs)
            decisions = tuple(replace(d, source_review={
                **d.source_review, "verdict": "reject",
                "checks": {**d.source_review["checks"], "privacy": False},
            }) for d in result.decisions)
            return replace(result, decisions=decisions)

        fixture.consolidator.consolidate = privacy_review
    _candidate(fixture, capture, **changes)
    result = fixture.runner.run(stage="consolidate")
    assert fixture.consolidator.calls[0][2] == "project:wezbridge"
    decision = fixture.runner._captures(fixture.runner._rows())[0]["decisions"][0]
    assert decision["action"] == ("ignore" if signal == "worktree" else "add")
    assert result["consolidate_guards"]["rescoped"] == 1
    population = fixture.runner._population(fixture.runner._rows())
    if signal == "worktree":
        assert not population  # Existing source-review privacy policy still rejects absolute paths.
    else:
        assert population[0]["source_context"]["capture_scope"] == "project:wezbridge"
    # Re-running keeps counts stable and reuses the reviewed decisions.
    fixture.runner.run(stage="consolidate")
    assert len(fixture.consolidator.calls) == 1


def test_rescoped_candidate_is_applied_in_reviewed_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", "1")
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"aliases": {"bridge-checkout": "project:wezbridge"}}))
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(aliases))
    fixture, capture = _extracted(tmp_path)
    _candidate(fixture, capture, subject="bridge-checkout")
    fixture.runner.run(stage="consolidate")
    fixture.runner.run(stage="quality")
    fixture.runner.run(stage="apply", apply_candidates=True)
    claims = fixture.service.list_claims(status="candidate", requesting_agent="dream-worker")
    assert len(claims) == 1 and claims[0].scope == "project:wezbridge"


def test_no_repo_signal_retains_inventory_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", "1")
    fixture, _ = _extracted(tmp_path)
    fixture.runner.run(stage="consolidate")
    assert fixture.consolidator.calls[0][2] == "project:repo"


def test_bare_name_of_known_repo_resolves_without_folder_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", "1")
    fixture, capture = _extracted(tmp_path)
    fixture.service.ingest(text="Relay deliveries retain recipient identity.",
                           scope="project:wezbridge", source_agent="fixture")
    _candidate(fixture, capture, subject="wezbridge")
    result = fixture.runner.run(stage="consolidate")
    assert fixture.consolidator.calls[0][2] == "project:wezbridge"
    assert result["consolidate_guards"]["rescoped"] == 1


def test_generic_rule_is_classified_and_counted(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", "1")
    fixture, capture = _extracted(tmp_path)
    candidate = _candidate(fixture, capture, text="Always verify evidence before making a claim.",
                           subject="agents", predicate="must", object_value="verify evidence",
                           evidence_quote="Always verify evidence before making a claim.")
    result = fixture.runner.run(stage="consolidate")
    assert not fixture.consolidator.calls
    assert result["consolidate_guards"]["generic"] == 1
    metadata = fixture.runner._rows()[0]["metadata"]["consolidate_guards"]
    assert metadata["candidates"][str(capture["id"])][candidate["candidate_id"]]["scope_class"] == "generic"
    assert fixture.runner._cohort_complete(fixture.runner._rows(), decisions=True)


def test_normalized_duplicate_is_skipped_and_counted(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", "1")
    fixture, capture = _extracted(tmp_path)
    text = capture["extraction"][0]["text"]
    fixture.service.ingest(text="  " + text.upper().replace(" ", "  ") + "  ",
                           scope="project:repo", source_agent="fixture")
    result = fixture.runner.run(stage="consolidate")
    assert not fixture.consolidator.calls
    assert result["consolidate_guards"]["duplicate"] == 1
    assert read_backfill_status(fixture.runner.config.state_dir)["consolidate_guards"]["duplicate"] == 1


@pytest.mark.parametrize("field", ["text", "subject", "predicate", "object_value", "evidence_quote"])
def test_sensitive_candidate_is_rejected_without_persisting_text(tmp_path, monkeypatch, field):
    monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", "1")
    fixture, _ = _extracted(tmp_path)
    secret = "Contact alice@example.com for the incident."
    # Inject an old, unsafe extraction at the read boundary; never seed it on disk.
    original = fixture.runner._captures

    def unsafe(rows):
        captures = original(rows)
        captures[0]["extraction"][0][field] = secret
        return captures

    monkeypatch.setattr(fixture.runner, "_captures", unsafe)
    result = fixture.runner.run(stage="consolidate")
    assert not fixture.consolidator.calls
    assert result["consolidate_guards"]["sensitive"] == 1
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


@pytest.mark.parametrize("flag", [None, "0"])
def test_flag_off_preserves_consolidation(tmp_path, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", raising=False)
    else:
        monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", flag)
    fixture, capture = _extracted(tmp_path)
    _candidate(fixture, capture, subject="wezbridge", text="Always verify evidence before making a claim.")
    result = fixture.runner.run(stage="consolidate")
    assert fixture.consolidator.calls[0][2] == "project:repo"
    assert "consolidate_guards" not in result


def test_enabling_guards_replaces_existing_unguarded_decisions(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", raising=False)
    fixture, capture = _extracted(tmp_path)
    _candidate(fixture, capture, subject="agents", predicate="must",
               text="Always verify evidence before making a claim.",
               evidence_quote="Always verify evidence before making a claim.")
    fixture.runner.run(stage="consolidate")
    assert len(fixture.runner._population(fixture.runner._rows())) == 1
    monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", "1")
    result = fixture.runner.run(stage="consolidate")
    assert result["consolidate_guards"]["generic"] == 1
    assert not fixture.runner._population(fixture.runner._rows())


def test_duplicate_filter_preserves_other_scopes_and_tenants(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", "1")
    fixture, capture = _extracted(tmp_path)
    text = capture["extraction"][0]["text"]
    fixture.service.ingest(text=text, scope="project:elsewhere", source_agent="fixture")
    from memorymaster.core.service import MemoryService

    other = MemoryService(fixture.db, workspace_root=fixture.repo, tenant_id="other")
    other.ingest(text=text, scope="project:repo", source_agent="fixture")
    result = fixture.runner.run(stage="consolidate")
    assert len(fixture.consolidator.calls) == 1
    assert result["consolidate_guards"]["duplicate"] == 0


def test_scope_alias_does_not_match_inside_another_word(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", "1")
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"aliases": {"bridge": "project:wezbridge"}}))
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(aliases))
    fixture, capture = _extracted(tmp_path)
    _candidate(fixture, capture, subject="cambridge", evidence_quote="The cambridge schema retains citations.")
    fixture.runner.run(stage="consolidate")
    assert fixture.consolidator.calls[0][2] == "project:repo"


def test_concrete_decision_without_repo_signal_keeps_inventory_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", "1")
    fixture, capture = _extracted(tmp_path)
    _candidate(fixture, capture, subject="storage", text="Use SQLite WAL for concurrent readers.",
               evidence_quote="Use SQLite WAL for concurrent readers.")
    fixture.runner.run(stage="consolidate")
    assert fixture.consolidator.calls[0][2] == "project:repo"


def test_rule_using_this_repos_tool_is_not_generic(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", "1")
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"aliases": {"repo": "project:memorymaster"}}))
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(aliases))
    fixture, capture = _extracted(tmp_path)
    _candidate(fixture, capture, subject="agents", text="Always call recall() before reusing a claim.",
               evidence_quote="Always call recall() before reusing a claim.")
    result = fixture.runner.run(stage="consolidate")
    assert fixture.consolidator.calls[0][2] == "project:memorymaster"
    assert result["consolidate_guards"]["generic"] == 0


def test_ambiguous_repo_signals_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", "1")
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"aliases": {"alpha": "project:alpha", "beta": "project:beta"}}))
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(aliases))
    fixture, capture = _extracted(tmp_path)
    _candidate(fixture, capture, subject="alpha", evidence_quote="Use beta for delivery.")
    result = fixture.runner.run(stage="consolidate")
    assert not fixture.consolidator.calls
    assert result["consolidate_guards"]["ambiguous_scope"] == 1


def test_duplicate_lookup_continues_after_empty_authorized_page(tmp_path, monkeypatch):
    from memorymaster.core.service import MemoryService

    monkeypatch.setenv("MEMORYMASTER_BACKFILL_CONSOLIDATE_GUARDS", "1")
    fixture, capture = _extracted(tmp_path)
    fixture.service.ingest(text=capture["extraction"][0]["text"], scope="project:repo", source_agent="fixture")
    original = MemoryService.list_claims_page
    cursors = []

    def pages(self, **kwargs):
        if kwargs["status"] == "candidate":
            cursors.append(kwargs["cursor"])
            if not kwargs["cursor"]:
                return [], "next"
            kwargs["cursor"] = ""
        return original(self, **kwargs)

    monkeypatch.setattr(MemoryService, "list_claims_page", pages)
    result = fixture.runner.run(stage="consolidate")
    assert cursors == ["", "next"]
    assert not fixture.consolidator.calls
    assert result["consolidate_guards"]["duplicate"] == 1
