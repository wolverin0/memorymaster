"""Ambient-write spool tests (P1 WAL-discipline spec step 9, §2.3).

WHY: the Stop hook (verbatim turns + extracted learnings) and the dream
bridge were two of the ~12 concurrent writers on the 3.47 GB SQLite file
that suffered real btree corruption (2026-06-05) — and the corruption was
confined to an index on verbatim_memories, the exact table the Stop hook
writes per stop. Under ``MEMORYMASTER_WAL_DISCIPLINE=1`` these ambient
writers leave the writer set entirely: they append spool envelopes (~10 ms)
and the steward drain replays them through the NORMAL service paths. These
tests pin the load-bearing requirements:

- the installed hook templates write valid v1 envelopes under the flag and
  never open/create the DB (the latency + lock win is the point);
- flag off = the untouched legacy direct-write path (the §5 rollback);
- drained verbatim lands in ``verbatim_memories`` with rows identical to
  the direct path (the spool is a transport, not a transform);
- a credential never reaches the plaintext spool at rest (sensitivity
  filter fires BEFORE the append, on top of the drain-time filter).

NOTE: the spec text says rows land in ``verbatim_turns``; the real table
(live DDL, verified in verbatim_store.py) is ``verbatim_memories``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from memorymaster.core import spool
from memorymaster.stores._storage_shared import open_conn
from memorymaster.bridges.dream_bridge import dream_ingest
from memorymaster.govern.jobs import spool_drain
from memorymaster.core.service import MemoryService
from memorymaster.recall.verbatim_store import (
    ensure_verbatim_schema,
    spool_transcript,
    store_transcript,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "memorymaster" / "config_templates" / "hooks"

# Mixed-role turns for the library-level parity tests. Every content is >20
# chars (store_verbatim's minimum) and carries no correction keywords.
TURNS = [
    ("user", "How does the WAL checkpoint discipline interact with pane churn?"),
    ("assistant", "The steward truncates the WAL every cycle so it stays bounded."),
    ("user", "And what happens when the spool drain replays a duplicate envelope?"),
]

# User-only turns for the subprocess template tests: with zero assistant
# turns the hook's Gemini-extraction and rule-mining paths return before any
# LLM call, keeping the subprocess offline and fast.
USER_TURNS = [
    ("user", "Please summarize the steward cycle phases for me today."),
    ("user", "How does the spool drainer report its lag seconds metric?"),
    ("user", "Which table stores the verbatim conversation history rows?"),
]


def _write_transcript(path: Path, turns: list[tuple[str, str]]) -> Path:
    lines = [json.dumps({"message": {"role": r, "content": c}}) for r, c in turns]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _envelopes(db_path: Path | str) -> list[dict]:
    spool_dir = spool.spool_dir_for(db_path)
    out: list[dict] = []
    if spool_dir.exists():
        for path in sorted(spool_dir.glob("*.jsonl")):
            for raw in path.read_text(encoding="utf-8").splitlines():
                out.append(json.loads(raw))
    return out


def _render_template(name: str, project_root: Path, dest: Path) -> Path:
    """Substitute the installer's placeholder like scripts/setup-hooks.py does.

    Forward slashes keep the rendered Windows path a valid Python string
    literal (raw backslashes like ``\\U`` would be invalid escapes).
    """
    text = (HOOKS_DIR / name).read_text(encoding="utf-8")
    rendered = text.replace(
        "__MEMORYMASTER_PROJECT_ROOT__", str(project_root).replace("\\", "/")
    )
    dest.write_text(rendered, encoding="utf-8")
    return dest


def _hook_env(tmp_home: Path, spool_root: Path, flag: str) -> dict[str, str]:
    env = os.environ.copy()
    # No provider keys may leak into the subprocess — the hook must stay
    # offline (its LLM paths early-return on user-only transcripts anyway).
    for key in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "QDRANT_URL"):
        env.pop(key, None)
    env["MEMORYMASTER_SPOOL_DIR"] = str(spool_root)
    env["MEMORYMASTER_WAL_DISCIPLINE"] = flag
    # Redirect home so STATE_DIR / hook logs / dream locks stay in the test tree.
    env["USERPROFILE"] = str(tmp_home)
    env["HOME"] = str(tmp_home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def _run_hook(hook: Path, stdin_payload: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(hook)],
        input=stdin_payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _import_template(project_root: Path, dest: Path, module_name: str):
    rendered = _render_template("memorymaster-auto-ingest.py", project_root, dest)
    spec = importlib.util.spec_from_file_location(module_name, rendered)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def spool_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated spool root; WAL-discipline flag cleared (conftest also clears
    it, but the dependency is load-bearing for these tests, so pin it here)."""
    root = tmp_path / "spool-root"
    monkeypatch.setenv(spool.ENV_SPOOL_DIR, str(root))
    monkeypatch.delenv("MEMORYMASTER_WAL_DISCIPLINE", raising=False)
    return root


# ---------------------------------------------------------------------------
# Library-level: verbatim spool path
# ---------------------------------------------------------------------------

def test_spool_transcript_writes_valid_envelopes_and_never_opens_db(
    tmp_path: Path, spool_root: Path
) -> None:
    """REQUIREMENT (spec §2.3): the Stop hook's latency/lock win is real only
    if the spool path NEVER touches the DB file — a multi-GB open per stop is
    exactly what put this hook in the 12-writer set. Each kept turn must
    become one valid v1 op:"verbatim" envelope carrying everything
    store_verbatim needs at drain time."""
    db = tmp_path / "ambient.db"  # deliberately never created
    transcript = _write_transcript(tmp_path / "session-abc.jsonl", TURNS)

    stats = spool_transcript(str(db), transcript, scope="project:demo", source_agent="stop-hook")

    assert stats == {"spooled": 3, "skipped": 0}
    assert not db.exists()  # the whole point: no DB open, no DB create
    envelopes = _envelopes(db)
    assert len(envelopes) == 3
    for envelope, (role, content) in zip(envelopes, TURNS):
        assert envelope["v"] == spool.SPOOL_VERSION
        assert envelope["op"] == "verbatim"
        payload = envelope["payload"]
        assert payload["session_id"] == "session-abc"
        assert (payload["role"], payload["content"]) == (role, content)
        assert payload["scope"] == "project:demo"
        assert payload["source_agent"] == "stop-hook"
        assert payload["timestamp"]


def test_sensitive_turn_never_reaches_spool_at_rest(
    tmp_path: Path, spool_root: Path
) -> None:
    """REQUIREMENT (.claude/rules/sensitivity-filter.md): the spool is a
    plaintext JSONL at rest — a credential must be dropped BEFORE the append,
    not merely at drain time, or the spool itself becomes the secret store
    the filter exists to prevent."""
    db = tmp_path / "ambient.db"
    secret = "sk-LiveSecret1234567890abcd"
    transcript = _write_transcript(
        tmp_path / "leak.jsonl",
        [("user", f"please remember my OPENAI key {secret} for later runs")],
    )

    stats = spool_transcript(str(db), transcript)

    assert stats == {"spooled": 0, "skipped": 1}
    spool_dir = spool.spool_dir_for(db)
    on_disk = "".join(
        p.read_text(encoding="utf-8") for p in spool_dir.rglob("*.jsonl")
    ) if spool_dir.exists() else ""
    assert secret not in on_disk


def test_drained_verbatim_rows_identical_to_direct_path(
    tmp_path: Path, spool_root: Path
) -> None:
    """REQUIREMENT (spec step 9 row parity): flag on vs flag off must produce
    the SAME verbatim_memories rows — the spool is a transport, not a
    transform. Any column drift (scope, source_agent, role filtering, dedup)
    would mean flipping the flag silently changes what verbatim recall can
    find. (Spec names the table 'verbatim_turns'; the real table is
    verbatim_memories.)"""
    transcript = _write_transcript(tmp_path / "sess-parity.jsonl", TURNS)
    cols = "session_id, role, content, scope, source_agent, embedding_synced"

    direct_db = tmp_path / "direct.db"
    ensure_verbatim_schema(str(direct_db))
    direct_stats = store_transcript(
        str(direct_db), transcript, scope="project:demo", source_agent="stop-hook"
    )
    assert direct_stats["stored"] == 3

    spooled_db = tmp_path / "spooled.db"
    svc = MemoryService(spooled_db)
    svc.init_db()
    spool_transcript(str(spooled_db), transcript, scope="project:demo", source_agent="stop-hook")
    drained = spool_drain.run(svc)
    assert drained["quarantined"] == 0
    assert drained["by_op"] == {"verbatim": 3}

    with open_conn(direct_db) as a, open_conn(spooled_db) as b:
        rows_direct = [tuple(r) for r in a.execute(
            f"SELECT {cols} FROM verbatim_memories ORDER BY id"
        ).fetchall()]
        rows_spooled = [tuple(r) for r in b.execute(
            f"SELECT {cols} FROM verbatim_memories ORDER BY id"
        ).fetchall()]
    assert rows_direct  # parity must not be vacuous
    assert rows_direct == rows_spooled


def test_respooled_transcript_dedups_at_drain(
    tmp_path: Path, spool_root: Path
) -> None:
    """REQUIREMENT: the Stop hook fires on EVERY stop and re-spools the whole
    transcript each time; store_verbatim's per-session content dedup must
    collapse the replays at drain time, or verbatim_memories regrows the
    9M-row duplicate pathology this dedup was built to kill (mm-0c43)."""
    transcript = _write_transcript(tmp_path / "sess-dup.jsonl", TURNS)
    db = tmp_path / "dedup.db"
    svc = MemoryService(db)
    svc.init_db()

    spool_transcript(str(db), transcript)
    spool_transcript(str(db), transcript)  # second stop, same session
    drained = spool_drain.run(svc)
    assert drained["by_op"] == {"verbatim": 6}
    assert drained["quarantined"] == 0

    with svc.store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM verbatim_memories").fetchone()[0]
    assert count == 3  # replays deduped, not duplicated


# ---------------------------------------------------------------------------
# Library-level: dream-bridge spool path
# ---------------------------------------------------------------------------

def _write_dream_memory(memdir: Path) -> None:
    (memdir / "build-cache.md").write_text(
        '---\nname: "Build cache behavior"\ntype: "project"\n---\n\n'
        "The build pipeline caches wheels between runs to speed cold installs.\n",
        encoding="utf-8",
    )


def test_dream_ingest_spools_dream_envelopes_under_flag(
    tmp_path: Path, spool_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQUIREMENT (spec §2.3): under the flag the dream bridge leaves the
    writer set — dream_ingest appends op:"dream" envelopes keyed
    auto-dream:<file> with the DB untouched; the drain replays them through
    svc.ingest, whose idempotency dedup makes the per-session-end re-spool a
    no-op (double spool+drain → ONE claim). A credential-bearing memory file
    must never reach the spool at all."""
    memdir = tmp_path / "memory"
    memdir.mkdir()
    _write_dream_memory(memdir)
    (memdir / "leaky.md").write_text(
        '---\nname: "Leaky"\ntype: "project"\n---\n\n'
        "Authenticate with sk-LiveSecret1234567890abcd before deploying anything.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_MEMORY_DIR", str(memdir))
    monkeypatch.setenv("MEMORYMASTER_WAL_DISCIPLINE", "1")

    db = tmp_path / "dream.db"
    svc = MemoryService(db)
    svc.init_db()

    stats = dream_ingest(str(db))
    assert stats["spooled"] == 1
    assert stats["ingested"] == 0
    assert stats["skipped"] >= 1  # the credential file was dropped pre-spool

    envelopes = _envelopes(db)
    assert [e["op"] for e in envelopes] == ["dream"]
    assert envelopes[0]["idempotency_key"] == "auto-dream:build-cache.md"
    assert "sk-LiveSecret" not in json.dumps(envelopes)
    with svc.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0

    assert spool_drain.run(svc)["drained"] == 1
    stats_again = dream_ingest(str(db))  # next session end re-spools the file
    assert stats_again["spooled"] == 1
    assert spool_drain.run(svc)["drained"] == 1  # replays clean (dedup, no error)

    with svc.store.connect() as conn:
        rows = conn.execute("SELECT idempotency_key FROM claims").fetchall()
    assert [r[0] for r in rows] == ["auto-dream:build-cache.md"]


def test_dream_ingest_flag_off_keeps_direct_insert_path(
    tmp_path: Path, spool_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQUIREMENT (governance: legacy path is the intact else-branch): with
    the flag unset dream_ingest INSERTs directly — claim row present, spool
    empty — so `setx MEMORYMASTER_WAL_DISCIPLINE 0` is a real rollback."""
    memdir = tmp_path / "memory"
    memdir.mkdir()
    _write_dream_memory(memdir)
    monkeypatch.setenv("CLAUDE_MEMORY_DIR", str(memdir))

    db = tmp_path / "dream-direct.db"
    MemoryService(db).init_db()

    stats = dream_ingest(str(db))
    assert stats["ingested"] == 1
    assert "spooled" not in stats
    assert spool.pending_depth(db) == {"files": 0, "lines": 0}
    with open_conn(db) as conn:
        rows = conn.execute("SELECT idempotency_key FROM claims").fetchall()
    assert [r[0] for r in rows] == ["auto-dream:build-cache.md"]


# ---------------------------------------------------------------------------
# Installed hook templates (the artifacts setup-hooks.py actually ships)
# ---------------------------------------------------------------------------

def test_auto_ingest_hook_template_spools_under_flag(
    tmp_path: Path, spool_root: Path
) -> None:
    """REQUIREMENT (spec step 9): the INSTALLED Stop-hook template — not just
    the library — must honor the inherited env flag: verbatim turns land as
    valid spool envelopes and the hook never creates/opens the DB (removing
    the per-stop multi-GB open is what the flag is for)."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    hook = _render_template("memorymaster-auto-ingest.py", project_root, tmp_path / "hook-on.py")
    transcript = _write_transcript(tmp_path / "sess-tpl.jsonl", USER_TURNS)
    payload = json.dumps({
        "session_id": "tplsess",
        "transcript_path": str(transcript),
        "cwd": str(project_root),
        "stop_hook_active": False,
    })

    env = _hook_env(tmp_path / "home", spool_root, "1")
    env["MEMORYMASTER_STOP_CAPTURE_VERBATIM"] = "1"
    proc = _run_hook(hook, payload, env)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["decision"] == "approve"
    db = project_root / "memorymaster.db"
    assert not db.exists()  # the hook never opened/created the DB
    envelopes = _envelopes(db)
    assert len(envelopes) == len(USER_TURNS)
    assert all(e["v"] == 1 and e["op"] == "verbatim" for e in envelopes)
    assert {e["payload"]["content"] for e in envelopes} == {c for _, c in USER_TURNS}


def test_auto_ingest_hook_template_direct_path_with_flag_off(
    tmp_path: Path, spool_root: Path
) -> None:
    """Explicit verbatim capture with WAL discipline off uses the direct path."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    db = project_root / "memorymaster.db"
    ensure_verbatim_schema(str(db))
    hook = _render_template("memorymaster-auto-ingest.py", project_root, tmp_path / "hook-off.py")
    transcript = _write_transcript(tmp_path / "sess-tpl-off.jsonl", USER_TURNS)
    payload = json.dumps({
        "session_id": "tplsessoff",
        "transcript_path": str(transcript),
        "cwd": str(project_root),
        "stop_hook_active": False,
    })

    env = _hook_env(tmp_path / "home", spool_root, "0")
    env["MEMORYMASTER_STOP_CAPTURE_VERBATIM"] = "1"
    proc = _run_hook(hook, payload, env)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["decision"] == "approve"
    with open_conn(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM verbatim_memories").fetchone()[0]
    assert count == len(USER_TURNS)
    assert spool.pending_depth(db) == {"files": 0, "lines": 0}


def test_dream_sync_hook_template_spools_under_flag(
    tmp_path: Path, spool_root: Path
) -> None:
    """REQUIREMENT (spec step 9): the installed dream-sync Stop hook under the
    flag must emit op:"dream" envelopes via dream_ingest instead of INSERTing
    — the template needs only the inherited env var, no template-side DB
    code, so flipping the flag requires no hook reinstall."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    db = project_root / "memorymaster.db"
    MemoryService(db).init_db()  # the dream_seed half still reads the DB
    memdir = tmp_path / "memory"
    memdir.mkdir()
    _write_dream_memory(memdir)
    hook = _render_template("memorymaster-dream-sync.py", project_root, tmp_path / "dream-on.py")
    env = _hook_env(tmp_path / "home", spool_root, "1")
    env["CLAUDE_MEMORY_DIR"] = str(memdir)

    proc = _run_hook(hook, "{}", env)

    assert proc.returncode == 0, proc.stderr
    assert "dream-sync error" not in proc.stderr
    envelopes = _envelopes(db)
    assert [e["op"] for e in envelopes] == ["dream"]
    assert envelopes[0]["idempotency_key"] == "auto-dream:build-cache.md"
    with open_conn(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_auto_ingest_template_spools_llm_learnings_as_ingest_envelopes(
    tmp_path: Path, spool_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQUIREMENT (spec §2.3 'Stop-hook learnings'): extracted learnings must
    become op:"ingest" envelopes under the flag — drained later through
    svc.ingest where the canonical sensitivity filter + idempotency dedup
    apply — instead of an in-hook MemoryService DB open."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MEMORYMASTER_WAL_DISCIPLINE", "1")
    module = _import_template(project_root, tmp_path / "auto_ingest_mod.py", "mm_tpl_learnings")

    import memorymaster.core.llm_provider as llm_provider
    fake_learning = json.dumps([{
        "text": "The steward cycle truncates the WAL before taking snapshots",
        "claim_type": "fact",
        "subject": "steward",
        "predicate": "behavior",
    }])
    monkeypatch.setattr(llm_provider, "call_llm", lambda *a, **k: fake_learning)
    transcript = _write_transcript(tmp_path / "sess-llm.jsonl", TURNS)

    module._run_gemini_extraction(str(transcript), str(project_root))

    db = project_root / "memorymaster.db"
    assert not db.exists()  # learnings spooled, DB never opened
    envelopes = _envelopes(db)
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope["op"] == "ingest"
    assert envelope["idempotency_key"].startswith("llm-stop-")
    assert envelope["payload"]["source_agent"] == "llm-stop-hook"
    assert envelope["payload"]["citations"][0]["source"] == "llm-stop-hook"


def test_template_flag_semantics_match_library(
    tmp_path: Path, spool_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQUIREMENT (drift guard): the template parses the flag locally (so the
    gate works before the package import resolves) — its truthiness table
    must stay identical to spool.wal_discipline_enabled, or a value like
    'off' would split the fleet between regimes."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    module = _import_template(tmp_path / "proj-flag", tmp_path / "auto_ingest_flag.py", "mm_tpl_flag")
    cases = [("", False), ("0", False), ("false", False), ("off", False),
             ("no", False), ("1", True), ("true", True), ("yes", True)]
    for value, expected in cases:
        monkeypatch.setenv("MEMORYMASTER_WAL_DISCIPLINE", value)
        assert module._spool_enabled() == expected, value
        assert spool.wal_discipline_enabled() == expected, value
