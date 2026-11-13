"""T-2278 Track B acceptance on synthetic data: 4 projects x {Claude, Codex, agy, OpenCode} SessionStart consumers.

Port of the audit's brief2.py 16-cell idea: a real-schema tmp DB with four project scopes (one with 40,000
claims), a tmp ledger and ROADMAP.md per project, the flags on in a tmp HOME, the hooks rendered from the shipped
templates. Every cell must show the running card, the next roadmap item and three recent failures whose labels
resolve to a claim. Nothing here reads the live database, ledger or any home directory.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import sqlite3
import sys
import time

import pytest

from memorymaster.core.service import MemoryService
from memorymaster.profile import project_brief, project_runtime
from memorymaster.profile.project_projection import (
    build_project_projection,
    read_manifest,
    read_project_projection,
)
from memorymaster.profile.project_repository import claim_label, load_brief_claims, load_project_snapshot, snapshot_since

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "memorymaster/config_templates/hooks"
SLUGS = ("memorymaster", "wezbridge", "infra", "futuramax")
BIG, OVERFLOW = "memorymaster", "futuramax"  # 40,000 claims; > 512 eligible claims (the legacy scan overflows)
FILLER = {"memorymaster": 40_000, "wezbridge": 3_000, "infra": 200, "futuramax": 600}
ELIGIBLE_NOISE = {"memorymaster": 120, "wezbridge": 80, "infra": 40, "futuramax": 600}
NOW = datetime.now(timezone.utc)
LONG = "long text that fills the whole cell so the brief is as wide as it can be, never short "


def stamp(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


def seed(db: Path, slug: str) -> dict[str, list[str]]:
    """One project's claims through raw executemany (the store insert path is far too slow for 40k rows)."""
    scope = f"project:{slug}"
    rows, cites, labels = [], [], {"failure": [], "decision": []}
    counter = iter(range(1, 10**6))

    def eligible(kind: str, text: str, age: int) -> None:
        n = next(counter)
        human = f"mm-{abs(hash((slug, n))) % 16**4:04x}"
        if human in seen:  # human_id is unique per (tenant, scope)
            human = f"mm-{n + 4096:05x}"
        seen.add(human)
        rows.append((text, scope, "confirmed", kind, stamp(age), stamp(age), stamp(age), human))
        cites.append((human, "session", f"session:{slug}:{n}", "evidence"))
        if kind in ("bug", "bug_cause", "gotcha", "decision"):
            labels["failure" if kind != "decision" else "decision"].append(human)

    seen: set[str] = set()
    # The newest rows of each kind: what the brief must show, newest first.
    for n, kind in enumerate(("bug", "bug_cause", "gotcha")):
        eligible(kind, f"{slug} failure {n} {LONG}", 1 + n)
    for n in range(3):
        eligible("decision", f"{slug} decision {n} {LONG}", 4 + n)
    # Older eligible rows that the legacy sections read and the brief must not need.
    for n in range(ELIGIBLE_NOISE[slug]):
        eligible("decision" if n % 2 else "gotcha", f"{slug} old noise {n}", 100 + n)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO claims(text,scope,status,claim_type,created_at,updated_at,event_time,human_id,source_agent)"
            " VALUES (?,?,?,?,?,?,?,?,'operator')", rows)
        conn.executemany(
            "INSERT INTO citations(claim_id,source,locator,excerpt,created_at) "
            f"SELECT id,?,?,?,'{stamp(0)}' FROM claims WHERE human_id=? AND scope='{scope}'",
            [(s, loc, ex, human) for human, s, loc, ex in cites])
        statuses = ("candidate", "stale", "archived", "candidate")
        conn.executemany(
            "INSERT INTO claims(text,scope,status,claim_type,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            [(f"filler {n}", scope, statuses[n % 4], "fact", stamp(30), stamp(30)) for n in range(FILLER[slug])])
    return labels


def ledger_card(slug: str, n: int, state: str) -> dict:
    return {"id": f"T-{n}{len(slug)}", "repo": slug, "state": state, "visibility": "public", "version": "1",
            "title": f"{slug} card {n} " + LONG, "next_action": LONG, "blocker": LONG,
            "updated_at": stamp(n)}


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """Seeded once (40k rows are slow); every test works on its own copy."""
    base = tmp_path_factory.mktemp("brief-template")
    db = base / "memorymaster.db"
    MemoryService(db, workspace_root=base).init_db()
    expected = {}
    for slug in SLUGS:
        root = base / "repos" / slug
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (root / "ROADMAP.md").write_text(f"## Now\n- [x] done item\n- [ ] {slug} next roadmap item\n")
        expected[slug] = seed(db, slug)
    ledger = base / "ledger"
    ledger.mkdir()
    (ledger / "tasks.json").write_text(json.dumps({"complete": True, "tasks": [
        ledger_card(slug, n, state) for slug in SLUGS for n, state in ((1, "running"), (2, "running"), (3, "ready"))]}))
    (base / "due.json").write_text("[]")
    return base, expected


@pytest.fixture
def world(template, tmp_path, monkeypatch):
    shutil.copytree(template[0], tmp_path / "w")
    tmp_path, expected = tmp_path / "w", template[1]
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    db = tmp_path / "memorymaster.db"
    service = MemoryService(db, workspace_root=tmp_path)
    ledger, due = tmp_path / "ledger", tmp_path / "due.json"
    projects = [{"db_path": str(db), "scope": f"project:{slug}", "tenant_id": None, "context_key": slug,
                 "project_root": str(tmp_path / "repos" / slug), "ledger_repo": slug, "ledger_filtered": True}
                for slug in SLUGS]
    env = {
        "MEMORYMASTER_PROJECT_PROJECTION_ENABLED": "1", "MEMORYMASTER_PROJECT_PROJECTION_REFRESH": "1",
        "MEMORYMASTER_PROJECT_PROJECTION_SESSION_START": "1", "MEMORYMASTER_PROJECT_PROJECTION_BRIEF": "1",
        "MEMORYMASTER_PROJECT_PROJECTION_PROJECTS": json.dumps(projects),
        "MEMORYMASTER_PROJECT_PROJECTION_EPOCH": "acceptance", "MEMORYMASTER_PROJECT_PROJECTION_DIR": str(tmp_path / "out"),
        "MEMORYMASTER_PROJECT_SESSION_STATE_DIR": str(tmp_path / "state"),
        "MEMORYMASTER_PROJECT_LEDGER_TASKS_DIR": str(ledger),
        "MEMORYMASTER_PROJECT_PROJECTION_DUE_MEASUREMENTS": str(due),
        "MEMORYMASTER_HOOK_EVENTS_LOG": str(tmp_path / "events.jsonl"),
        "MEMORYMASTER_RECALL_STATE_DIR": str(tmp_path / "recall"), "MEMORYMASTER_PROJECT_SESSION_DELTA": "0",
        "MEMORYMASTER_HOOK_RECALL_REMOTE": "0", "MEMORYMASTER_DEFAULT_DB": str(db),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    for name in ("MEMORYMASTER_INTERNAL_CODEX", "MEMORYMASTER_INTERNAL_RUN"):
        monkeypatch.delenv(name, raising=False)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("memorymaster-codex-recall.py", "memorymaster-recall.py", "memorymaster-session-start.py"):
        text = (TEMPLATES / name).read_text(encoding="utf-8").replace("__MEMORYMASTER_PROJECT_ROOT__", ROOT.as_posix())
        (hooks / name).write_text(text.replace('MM_ROOT / "memorymaster.db"', f"Path({str(db)!r})"), encoding="utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.chdir(tmp_path)
    project_runtime._refresh_cache.clear()
    yield service, hooks, expected, tmp_path
    for connection in project_runtime._connections.values():
        connection.close()
    project_runtime._connections.clear()
    project_runtime._refresh_cache.clear()


def run_agy(cwd: str, db: Path) -> str:
    from memorymaster.surfaces import agy_hooks

    raw = agy_hooks.run_hook("SessionStart", {"conversationId": "acceptance-agy", "workspacePaths": [cwd],
                                              "lastUserInput": ""}, db_path=str(db))
    return json.loads(raw)["injectSteps"][0]["ephemeralMessage"] if raw else ""


def run_opencode(cwd: str, monkeypatch) -> str:
    """The Python entry point the OpenCode plugin spawns with --session-start (the plugin itself: test_opencode_hooks)."""
    from memorymaster.surfaces import hook_recall

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": cwd, "session_id": "acceptance-opencode"})))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    assert hook_recall.main(["--cli", "opencode", "--session-start"]) == 0
    out = stdout.getvalue()
    assert not out or out.startswith(hook_recall.BRIEF_PREFIX), out[:80]
    return out


def run_hook(hooks: Path, cli: str, cwd: str, monkeypatch, db: Path | None = None) -> str:
    if cli == "agy":
        return run_agy(cwd, db)
    if cli == "opencode":
        return run_opencode(cwd, monkeypatch)
    data = json.dumps({"cwd": cwd, "session_id": f"acceptance-{cli}", "hook_event_name": "SessionStart"})
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(data))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.chdir(cwd)
    if cli == "codex":
        namespace = runpy.run_path(str(hooks / "memorymaster-codex-recall.py"))
        assert namespace["main"]("SessionStart") == 0
    else:
        try:
            runpy.run_path(str(hooks / "memorymaster-session-start.py"), run_name="__main__")
        except SystemExit:
            pass
    raw = out.getvalue().strip()
    return json.loads(raw)["hookSpecificOutput"]["additionalContext"] if raw else ""


def section(brief_text: str, title: str) -> list[str]:
    lines = brief_text.split("\n")
    start = lines.index(title) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].isupper()), len(lines))
    return [line for line in lines[start:end] if line.startswith("- ")]


def test_sixteen_cells_every_cli_shows_running_next_and_three_resolvable_failures(world, monkeypatch, capsys):
    service, hooks, expected, tmp_path = world
    configs = {config.context_key: config for config in project_runtime.load_project_configs()}
    assert set(configs) == set(SLUGS)
    for slug in SLUGS:
        result = project_runtime._refresh_one(configs[slug])
        assert result["status"] == "ok", f"{slug}: refresh did not publish ({result})"
    sizes = {}
    for slug in SLUGS:
        for cli in ("claude", "codex", "agy", "opencode"):
            context = run_hook(hooks, cli, str(tmp_path / "repos" / slug), monkeypatch, tmp_path / "memorymaster.db")
            cell = f"{slug}/{cli}"
            assert "Brief " + slug in context, f"{cell}: no brief in {context[:200]!r}"
            running = section(context, "RUNNING")
            assert running and running[0].startswith(f"- T-1{len(slug)} running: {slug} card 1"), f"{cell}: {running}"
            nxt = section(context, "NEXT")
            assert nxt == [f"- {slug} next roadmap item [ROADMAP.md#L3]"], f"{cell}: {nxt}"
            failures = section(context, "FAILURES")
            assert len(failures) == 3, f"{cell}: {failures}"
            for index, line in enumerate(failures):
                label = line.rsplit(" [", 1)[1].rstrip("]")
                assert label == expected[slug]["failure"][index], f"{cell}: newest failures first, got {label}"
                # Resolvable: the store's own human_id lookup returns the claim the line names.
                claim = service.store.get_claim_by_human_id(label, tenant_id=None, scope=f"project:{slug}")
                assert claim is not None and claim.text.split(" ")[0] == slug, f"{cell}: {label} does not resolve"
            sizes[cell] = len(context.encode())
    with capsys.disabled():
        print("brief context bytes per cell:", sizes)
        print("brief bytes per project:", {c: len(read_manifest(configs[c])["brief"].encode()) for c in SLUGS})
    assert max(sizes.values()) <= 3000
    assert max(size for cell, size in sizes.items() if not cell.endswith("claude")) <= 1800 + 64  # opencode: + its prefix


def test_large_scope_publishes_complete_and_overflow_publishes_degraded(world):
    _, _, _, _ = world
    configs = {config.context_key: config for config in project_runtime.load_project_configs()}
    big = build_project_projection(configs[BIG])
    # 40,000 claims in the scope, ~130 that can render: the brief-on scan reads only those.
    assert big.manifest["source_complete"] is True and "brief" in big.manifest, big.manifest["diagnostics"]
    over = build_project_projection(configs[OVERFLOW])
    assert over.manifest["source_complete"] is False and "brief" in over.manifest  # > 512 eligible: declared partial
    assert project_runtime._refresh_one(configs[OVERFLOW])["status"] == "ok"
    assert "Brief futuramax" in read_manifest(configs[OVERFLOW])["brief"]


def test_brief_off_still_refuses_an_incomplete_source(world):
    _, _, _, _ = world
    config = replace({c.context_key: c for c in project_runtime.load_project_configs()}[OVERFLOW], brief=False)
    assert project_runtime._refresh_one(config)["status"] == "pending"


# --- smaller contracts ----------------------------------------------------------------------------------------

def test_labels_are_human_ids_and_never_a_numeric_mm_id():
    assert claim_label({"id": 160535, "human_id": "mm-344e"}) == "mm-344e"
    assert claim_label({"id": 7, "human_id": "mm-29c2~2"}) == "mm-29c2~2"
    assert claim_label({"id": 7, "human_id": None}) == "claim:7"
    assert claim_label({"id": 7, "human_id": "x]\n## NEXT"}) == "claim:7"


def test_ledger_not_configured_is_one_explicit_line_and_an_unreadable_ledger_says_so(world):
    _, _, _, _ = world
    config = {c.context_key: c for c in project_runtime.load_project_configs()}["infra"]
    for changes, reason in ((dict(ledger_filtered=False), "ledger source not configured (mapping ledger_filtered"),
                            (dict(ledger_root=None), "ledger source not configured"),
                            (dict(ledger_root=config.ledger_root / "missing"), "ledger source unreadable")):
        brief = build_project_projection(replace(config, **changes)).manifest["brief"]
        assert f"RUNNING\n- unavailable: {reason}" in brief and brief.count("ledger unavailable") == 1, brief


def test_one_unreadable_claim_section_does_not_hide_the_other(world, monkeypatch):
    _, _, _, _ = world
    config = {c.context_key: c for c in project_runtime.load_project_configs()}["infra"]
    real = project_brief.load_brief_claims.__globals__["_brief_kind"]

    def flaky(limited, kind, *args):
        if kind == "failure":
            raise sqlite3.OperationalError("interrupted")
        return real(limited, kind, *args)

    monkeypatch.setitem(load_brief_claims.__globals__, "_brief_kind", flaky)
    found, ok = load_brief_claims(config)
    assert not ok and found["failure"] is None and len(found["decision"]) == 3
    brief = build_project_projection(config).manifest["brief"]
    assert "FAILURES\n- unavailable: claims read failed or timed out [claims]" in brief
    assert "claims partial" in brief.split("\n")[0] and section(brief, "DECISIONS")


def test_a_transient_failure_does_not_replace_a_fresh_complete_publication(world, monkeypatch):
    _, _, _, _ = world
    config = {c.context_key: c for c in project_runtime.load_project_configs()}["infra"]
    assert project_runtime._refresh_one(config)["status"] == "ok"
    good = read_manifest(config)
    assert good["source_complete"] is True
    monkeypatch.setattr("memorymaster.profile.project_repository.MAX_CLAIMS", 1)
    assert project_runtime._refresh_one(config)["status"] == "pending"
    assert read_manifest(config) == good
    stale = dict(good, checked_at=(NOW - timedelta(minutes=40)).isoformat())
    projection = build_project_projection(config)
    assert project_runtime._publish_degraded(config, projection.manifest, stale) is True


def test_brief_scan_watches_a_candidate_that_is_confirmed_later(world, monkeypatch):
    """The narrowed scan skips candidates; the change cursor must still deliver one that becomes eligible."""
    _, _, _, _ = world
    config = {c.context_key: c for c in project_runtime.load_project_configs()}["infra"]
    baseline = load_project_snapshot(config)
    assert baseline["complete"]
    with sqlite3.connect(config.db_path) as conn:
        row = conn.execute("SELECT id FROM claims WHERE scope=? AND status='candidate' LIMIT 1", (config.scope,)).fetchone()
        conn.execute("UPDATE claims SET status='confirmed',claim_type='decision',updated_at='2001-01-01T00:00:00+00:00'"
                     " WHERE id=?", (row[0],))
        conn.execute("INSERT INTO citations(claim_id,source,locator,excerpt,created_at) VALUES (?,?,?,?,?)",
                     (row[0], "session", "late", "evidence", stamp(0)))
    current = snapshot_since(config, baseline["coverage"], baseline["read_at"])
    assert f"claim:{row[0]}" in current["coverage"] and current["complete"]


# --- SessionStart validation reads only the published cards -----------------------------------------------------

def one_card_per_file_ledger(root: Path, repo: str, count: int) -> None:
    for existing in root.glob("*.json"):
        existing.unlink()
    for n in range(count):
        state = "running" if n < 3 else "done"
        (root / f"T-{n:04d}.json").write_text(json.dumps({
            "id": f"T-{n:04d}", "repo": repo, "state": state, "visibility": "public", "version": "1",
            "title": f"card {n}", "updated_at": stamp(n % 3)}))
        if state == "done":  # old closed cards are skipped by selection, like the live ledger's history
            old = time.time() - 100 * 86_400
            os.utime(root / f"T-{n:04d}.json", (old, old))


def test_validation_reads_only_the_published_cards_of_a_big_ledger(world, monkeypatch):
    _, _, _, tmp_path = world
    config = {c.context_key: c for c in project_runtime.load_project_configs()}["infra"]
    one_card_per_file_ledger(config.ledger_root, "infra", 2_500)
    assert project_runtime._refresh_one(config)["status"] == "ok"
    opened: list[str] = []
    real = Path.read_bytes

    def counting(self):
        opened.append(self.name)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    started = time.monotonic()
    projection = read_project_projection(config)
    elapsed = time.monotonic() - started
    published = {r["supports"][0]["id"] for r in read_manifest(config)["items"] if r["supports"][0]["kind"] == "card"}
    ledger_opens = {name for name in opened if name.startswith("T-")}
    assert projection is not None and published and ledger_opens == {f"{card}.json" for card in published}
    assert len(opened) <= 2 * len(published) and elapsed < 1.0  # kept files are hashed twice; far below 2,500 opens
    assert "Brief infra" in projection.payload and "not_revalidated" not in projection.payload


def test_validation_that_cannot_finish_serves_the_brief_with_a_not_revalidated_note(world, monkeypatch):
    _, _, _, tmp_path = world
    config = {c.context_key: c for c in project_runtime.load_project_configs()}["infra"]
    one_card_per_file_ledger(config.ledger_root, "infra", 50)
    assert project_runtime._refresh_one(config)["status"] == "ok"
    brief = read_manifest(config)["brief"]
    monkeypatch.setattr("memorymaster.profile.project_repository.VALIDATION_SECONDS", -1.0)
    projection = read_project_projection(config)
    assert projection is not None and brief in projection.payload
    assert "not_revalidated" in projection.payload


def test_builder_scan_is_not_bound_by_the_tight_sessionstart_deadline(world, monkeypatch):
    """Windows regression: the refresh-path scan shared the 0.1 s SessionStart deadline and, cold or loaded, came
    back claims_unavailable. A near-zero SessionStart deadline must not affect the builder."""
    _, _, _, _ = world
    config = {c.context_key: c for c in project_runtime.load_project_configs()}["infra"]
    from memorymaster.profile import project_repository as repository
    monkeypatch.setattr(repository, "VALIDATION_SECONDS", 1e-9)
    built = load_project_snapshot(config)
    assert "claims_unavailable" not in built["diagnostics"] and built["complete"], built["diagnostics"]
