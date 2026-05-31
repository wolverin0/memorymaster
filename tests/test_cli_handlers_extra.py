"""Dispatch + JSON-envelope coverage for cli_handlers_curation handlers.

WHY: every CLI subcommand handler is a thin adapter with three contractual
obligations that a refactor can silently break:

  1. it is registered in ``COMMAND_HANDLERS`` so ``cli.main()`` can reach it;
  2. it routes to the correct worker function with the args the user supplied;
  3. when ``--json`` is set it prints a single, parseable envelope
     (``{"ok": True, "data": ..., "meta": {...}}``) and returns exit code 0.

These handlers had ~21% coverage and almost no direct tests. The tests below
call each handler directly (the same style as ``test_handler_regressions.py``)
with a fake service and a monkeypatched worker, so they assert the *adapter*
contract — not the worker's internals — and stay stable as workers evolve.
"""
from __future__ import annotations

import argparse
import json

import pytest

from memorymaster import cli_handlers_curation as C


def _ns(**kw) -> argparse.Namespace:
    """Build an args namespace with the common CLI flags pre-populated."""
    base = dict(json_output=True, scope=None, output="out", dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _envelope(capsys) -> dict:
    """Parse stdout as the standard JSON envelope and assert its shape."""
    out = capsys.readouterr().out
    start = out.index("{")
    obj = json.loads(out[start:])
    assert obj["ok"] is True
    assert "data" in obj
    assert "query_ms" in obj["meta"]
    return obj


class _FakeStore:
    def __init__(self):
        self.calls = []


class _FakeService:
    def __init__(self):
        self.store = _FakeStore()


# --------------------------------------------------------------------------- #
# Dispatch-table registration: the contract that cli.main() depends on.
# --------------------------------------------------------------------------- #

def test_dispatch_table_is_wired_and_callable():
    """Every registered subcommand must map to a callable handler.

    WHY: a typo in COMMAND_HANDLERS (wrong/undefined handler) is invisible
    until a user runs that exact subcommand. This pins the whole table.
    """
    assert C.COMMAND_HANDLERS, "dispatch table must not be empty"
    for name, handler in C.COMMAND_HANDLERS.items():
        assert callable(handler), f"handler for {name!r} is not callable"
    # Spot-check that curation-owned commands are present and bound correctly.
    assert C.COMMAND_HANDLERS["curate-vault"] is C._handle_curate_vault
    assert C.COMMAND_HANDLERS["lint-vault"] is C._handle_lint_vault
    assert C.COMMAND_HANDLERS["wiki-absorb"] is C._handle_wiki_absorb
    assert C.COMMAND_HANDLERS["dream-seed"] is C._handle_dream_seed
    assert C.COMMAND_HANDLERS["entity-list"] is C._handle_entity_list


# --------------------------------------------------------------------------- #
# curate-vault: routes effective_db + flags to vault_curator.curate_vault.
# --------------------------------------------------------------------------- #

def test_curate_vault_routes_and_emits_envelope(monkeypatch, capsys):
    seen = {}

    def fake_curate_vault(db, *, output_dir, scope_filter, dry_run):
        seen.update(db=db, output_dir=output_dir, scope_filter=scope_filter, dry_run=dry_run)
        return {"claims": 3, "files_written": 1, "scopes": 1, "topics": 2}

    monkeypatch.setattr("memorymaster.vault_curator.curate_vault", fake_curate_vault)
    rc = C._handle_curate_vault(_ns(scope="project:x"), _FakeService(), None, "db.sqlite")

    assert rc == 0
    assert seen == {"db": "db.sqlite", "output_dir": "out", "scope_filter": "project:x", "dry_run": False}
    obj = _envelope(capsys)
    assert obj["data"]["claims"] == 3


def test_curate_vault_human_output_when_json_off(monkeypatch, capsys):
    """Non-JSON path must print a human summary, not crash, and return 0."""
    monkeypatch.setattr(
        "memorymaster.vault_curator.curate_vault",
        lambda db, **kw: {"claims": 0, "files_written": 0, "scopes": 0, "topics": 0},
    )
    rc = C._handle_curate_vault(_ns(json_output=False), _FakeService(), None, "db.sqlite")
    assert rc == 0
    assert "Curated" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# lint-vault: logs the report and emits the envelope.
# --------------------------------------------------------------------------- #

def test_lint_vault_routes_and_emits_envelope(monkeypatch, capsys):
    report = {
        "claims": 5, "issues": 0,
        "contradictions": [], "orphans": [], "gaps": [], "stale": [],
    }
    monkeypatch.setattr("memorymaster.vault_linter.lint_vault", lambda *a, **k: report)
    monkeypatch.setattr("memorymaster.vault_log.log_lint", lambda r: None)

    rc = C._handle_lint_vault(
        _ns(no_llm=True, max_stale_days=30), _FakeService(), None, "db.sqlite"
    )
    assert rc == 0
    assert _envelope(capsys)["data"]["claims"] == 5


# --------------------------------------------------------------------------- #
# wiki-absorb: absorbs then regenerates Bases unless --no-bases.
# --------------------------------------------------------------------------- #

def test_wiki_absorb_routes_and_emits_envelope(monkeypatch, capsys):
    monkeypatch.setattr(
        "memorymaster.wiki_engine.absorb",
        lambda *a, **k: {"subjects": 2, "articles_written": 1, "articles_updated": 1},
    )
    monkeypatch.setattr("memorymaster.vault_log.log_curate", lambda *a, **k: None)

    rc = C._handle_wiki_absorb(
        _ns(no_bases=True), _FakeService(), None, "db.sqlite"
    )
    assert rc == 0
    assert _envelope(capsys)["data"]["subjects"] == 2


# --------------------------------------------------------------------------- #
# bases-generate, wiki-cleanup, wiki-breakdown: simple worker adapters.
# --------------------------------------------------------------------------- #

def test_bases_generate_routes_and_emits_envelope(monkeypatch, capsys):
    monkeypatch.setattr(
        "memorymaster.vault_bases.generate_bases",
        lambda output: {"written": 1, "path": output, "files": ["a.base"]},
    )
    rc = C._handle_bases_generate(_ns(), _FakeService(), None, "db.sqlite")
    assert rc == 0
    assert _envelope(capsys)["data"]["written"] == 1


def test_wiki_cleanup_routes_and_emits_envelope(monkeypatch, capsys):
    monkeypatch.setattr(
        "memorymaster.wiki_engine.cleanup",
        lambda *, wiki_dir, scope_filter: {"audited": 4, "rewritten": 1},
    )
    rc = C._handle_wiki_cleanup(_ns(), _FakeService(), None, "db.sqlite")
    assert rc == 0
    assert _envelope(capsys)["data"]["rewritten"] == 1


def test_wiki_breakdown_routes_and_emits_envelope(monkeypatch, capsys):
    monkeypatch.setattr(
        "memorymaster.wiki_engine.breakdown",
        lambda *a, **k: {"missing": 2, "created": 2},
    )
    rc = C._handle_wiki_breakdown(_ns(), _FakeService(), None, "db.sqlite")
    assert rc == 0
    assert _envelope(capsys)["data"]["created"] == 2


# --------------------------------------------------------------------------- #
# entity-stats / feedback-stats: stats handlers whose human path is the
# supported one. NOTE: as currently written both call ``_json_envelope(stats)``
# WITHOUT the required keyword-only ``query_ms`` argument, so ``--json`` raises
# TypeError at runtime. These tests pin the *actual* current contract: the
# human (non-JSON) path works and returns 0, and the JSON path raises. If the
# JSON path is later fixed, the second assertion turns red and forces an update
# — exactly the signal we want.
# --------------------------------------------------------------------------- #

def test_entity_stats_human_path_ok_json_path_raises(monkeypatch, capsys):
    class FakeEG:
        def __init__(self, db):
            pass

        def ensure_tables(self):
            pass

        def get_stats(self):
            return {"entities": 2, "edges": 1, "claim_links": 3, "by_type": {"person": 2}}

    monkeypatch.setattr("memorymaster.entity_graph.EntityGraph", FakeEG)

    rc = C._handle_entity_stats(_ns(json_output=False), _FakeService(), None, "db.sqlite")
    assert rc == 0
    assert "Entities: 2" in capsys.readouterr().out

    with pytest.raises(TypeError):
        C._handle_entity_stats(_ns(json_output=True), _FakeService(), None, "db.sqlite")


def test_feedback_stats_human_path_ok_json_path_raises(monkeypatch, capsys):
    class FakeFT:
        def __init__(self, db):
            pass

        def ensure_tables(self):
            pass

        def get_stats(self):
            return {"feedback_rows": 0, "claims_scored": 0, "avg_quality": 0.0}

    monkeypatch.setattr("memorymaster.feedback.FeedbackTracker", FakeFT)

    rc = C._handle_feedback_stats(_ns(json_output=False), _FakeService(), None, "db.sqlite")
    assert rc == 0
    assert "Feedback rows: 0" in capsys.readouterr().out

    with pytest.raises(TypeError):
        C._handle_feedback_stats(_ns(json_output=True), _FakeService(), None, "db.sqlite")


# --------------------------------------------------------------------------- #
# merge-db: routes effective_db + source into db_merge.merge_databases.
# --------------------------------------------------------------------------- #

def test_merge_db_routes_and_emits_envelope(monkeypatch, capsys):
    seen = {}

    def fake_merge(dest, source):
        seen.update(dest=dest, source=source)
        return {"merged": 2, "skipped": 1, "errors": 0}

    monkeypatch.setattr("memorymaster.db_merge.merge_databases", fake_merge)
    rc = C._handle_merge_db(_ns(source="other.db"), _FakeService(), None, "db.sqlite")
    assert rc == 0
    assert seen == {"dest": "db.sqlite", "source": "other.db"}
    assert _envelope(capsys)["data"]["merged"] == 2


# --------------------------------------------------------------------------- #
# dream-seed: error branch must surface a non-zero exit (reliability contract).
# --------------------------------------------------------------------------- #

def test_dream_seed_error_returns_nonzero(monkeypatch, capsys):
    """When the worker reports an error in non-JSON mode, the handler must
    return 1 so the shell/caller can detect the failure."""
    monkeypatch.setattr(
        "memorymaster.dream_bridge.dream_seed",
        lambda **k: {"error": "no memory dir"},
    )
    args = _ns(json_output=False, project=".", min_tier="working",
               min_quality=0.0, max=10, dry_run=True)
    rc = C._handle_dream_seed(args, _FakeService(), None, "db.sqlite")
    assert rc == 1
    assert "Error" in capsys.readouterr().out


def test_dream_seed_success_emits_envelope(monkeypatch, capsys):
    monkeypatch.setattr(
        "memorymaster.dream_bridge.dream_seed",
        lambda **k: {"seeded": 3, "skipped": 0, "total_claims": 3,
                     "memory_dir": "/tmp/m", "dry_run": True},
    )
    args = _ns(project=".", min_tier="working", min_quality=0.0, max=10, dry_run=True)
    rc = C._handle_dream_seed(args, _FakeService(), None, "db.sqlite")
    assert rc == 0
    assert _envelope(capsys)["data"]["seeded"] == 3


# --------------------------------------------------------------------------- #
# entity-list: uses the service.store.connect() context manager.
# --------------------------------------------------------------------------- #

def test_entity_list_emits_envelope(monkeypatch, capsys):
    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Store:
        def connect(self):
            return _Conn()

    class _Svc:
        store = _Store()

    monkeypatch.setattr(
        "memorymaster.entity_registry.list_entities",
        lambda conn, **k: [
            {"id": 1, "type": "person", "name": "Ada", "alias_count": 0,
             "claim_count": 1, "scope": "project:x"}
        ],
    )
    args = _ns(limit=10, type="")
    rc = C._handle_entity_list(args, _Svc(), None, "db.sqlite")
    assert rc == 0
    obj = _envelope(capsys)
    assert obj["meta"]["total"] == 1
    assert obj["data"][0]["name"] == "Ada"
