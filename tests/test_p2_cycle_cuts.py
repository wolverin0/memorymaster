"""P2 phase0 cycle-cut pins — the 3 cuts that break the 10-module import SCC.

Cut 1: llm_provider must never import llm_steward. KeyRotator's real home is
       memorymaster.core.key_rotator (RoundRobinKeyRotator); llm_steward only
       re-exports it for backward compatibility.
Cut 2: lifecycle must never import wiki_engine. The wiki autopromote trigger
       is inverted into lifecycle.on_claim_confirmed, registered by wiring
       modules (service.py, wiki_engine.py).
Cut 3: llm_steward must never import store_factory. Auto-validation accepts an
       injected store; the default resolves via jobs.deterministic.open_store.

Each cut is pinned at three levels: AST source scan (no forbidden import
statement anywhere in the module, including lazy function-level imports),
subprocess import isolation (module imports cleanly with the forbidden target
blocked), and functional behavior.
"""
from __future__ import annotations

import ast
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "memorymaster"

FORBIDDEN_EDGES = [
    # (module file, forbidden import target) — one per cycle cut
    ("llm_provider.py", "llm_steward"),
    ("lifecycle.py", "wiki_engine"),
    ("llm_steward.py", "store_factory"),
]


def _imported_module_names(path: Path) -> set[str]:
    """All module names imported anywhere in the file (incl. lazy fn-level)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module_file,forbidden", FORBIDDEN_EDGES)
def test_forbidden_import_edge_is_cut(module_file: str, forbidden: str) -> None:
    """No import statement (top-level OR lazy) may reference the cut target.

    The census import-graph counts lazy function-level imports as SCC edges,
    so a 'hidden' lazy import would silently re-create the cycle.
    """
    imported = _imported_module_names(PKG / module_file)
    offenders = {name for name in imported if forbidden in name}
    assert not offenders, (
        f"{module_file} re-grew the cut import edge to {forbidden}: {offenders}"
    )


def _run_isolated(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=120,
    )


_BLOCKER = """
    import importlib.abc
    import sys

    BLOCKED = {blocked!r}

    class _Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == BLOCKED:
                raise ImportError(f"{{name}} blocked by P2 cycle-cut pin")
            return None

    sys.meta_path.insert(0, _Blocker())
"""


def test_lifecycle_imports_with_wiki_engine_blocked() -> None:
    """lifecycle must import and transition claims even if wiki_engine cannot
    be imported at all (the old code lazily imported it on the autopromote
    path; the hook default of None must make that path a clean no-op)."""
    code = _BLOCKER.format(blocked="memorymaster.knowledge.wiki_engine") + """
    import memorymaster.core.lifecycle as lifecycle

    assert "memorymaster.knowledge.wiki_engine" not in sys.modules
    assert lifecycle.on_claim_confirmed is None
    # The autopromote helper must be a silent no-op without a hook.
    class _Store:
        db_path = "unused.db"
        def list_events(self, **kwargs):
            raise AssertionError("threshold env unset path should not get here")
    lifecycle._wiki_autopromote_after_validator(_Store(), 1, "transition")
    print("OK")
    """
    proc = _run_isolated(code)
    assert proc.returncode == 0, proc.stderr


def test_llm_provider_env_rotation_with_llm_steward_blocked() -> None:
    """The env-rotation path (former home of the lazy llm_steward import)
    must work with llm_steward completely unimportable."""
    code = _BLOCKER.format(blocked="memorymaster.govern.llm_steward") + """
    import os
    os.environ["MEMORYMASTER_LLM_KEY_ROTATION"] = "1"
    os.environ["MEMORYMASTER_LLM_API_KEYS"] = "pin-key-1,pin-key-2"

    from memorymaster.core.llm_provider import _get_google_env_rotator
    from memorymaster.core.key_rotator import RoundRobinKeyRotator

    rotator = _get_google_env_rotator()
    assert isinstance(rotator, RoundRobinKeyRotator)
    assert rotator.key_count == 2
    assert rotator.get_key() == "pin-key-1"
    assert "memorymaster.govern.llm_steward" not in sys.modules
    print("OK")
    """
    proc = _run_isolated(code)
    assert proc.returncode == 0, proc.stderr


def test_llm_steward_imports_with_store_factory_blocked() -> None:
    """llm_steward must import (and its compat KeyRotator re-export must work)
    with store_factory completely unimportable."""
    code = _BLOCKER.format(blocked="memorymaster.stores.store_factory") + """
    from memorymaster.govern.llm_steward import (
        DEFAULT_COOLDOWN_SECONDS,
        KeyRotator,
        _auto_validate_claims,
    )
    from memorymaster.core.key_rotator import RoundRobinKeyRotator

    # Compat re-export: external callers keep importing KeyRotator from
    # llm_steward for one minor version.
    assert KeyRotator is RoundRobinKeyRotator
    assert DEFAULT_COOLDOWN_SECONDS == 60.0

    # Empty-ids fast path needs no store at all.
    result = _auto_validate_claims("unused.db", [])
    assert result == {"checked": 0, "boosted": 0, "dropped": 0, "hard_conflicted": 0}
    assert "memorymaster.stores.store_factory" not in sys.modules
    print("OK")
    """
    proc = _run_isolated(code)
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# Functional pins (in-process)
# ---------------------------------------------------------------------------


def _fresh_db(tmp_path: Path):
    from memorymaster.stores.storage import SQLiteStore

    db = tmp_path / "memory.db"
    store = SQLiteStore(str(db))
    store.init_db()
    conn = sqlite3.connect(str(db))
    cur = conn.execute(
        """INSERT INTO claims (text, claim_type, subject, predicate, object_value,
                               scope, status, confidence, created_at, updated_at,
                               valid_from, tier, version)
           VALUES ('hook pin claim', 'fact', 'pin', 'checks', 'hook', 'project:test',
                   'candidate', 0.8, '2026-01-01', '2026-01-01', '2026-01-01',
                   'working', 1)""",
    )
    conn.commit()
    claim_id = int(cur.lastrowid)
    conn.close()
    return store, db, claim_id


def test_lifecycle_autopromote_fires_registered_hook(tmp_path: Path, monkeypatch) -> None:
    """transition_claim must route autopromote through on_claim_confirmed —
    anchoring the inversion: lifecycle calls whatever was registered, it never
    resolves wiki_engine itself."""
    from memorymaster.core import lifecycle
    from memorymaster.core.lifecycle import transition_claim

    store, db, claim_id = _fresh_db(tmp_path)
    calls: list[tuple[int, str | None]] = []

    monkeypatch.setenv("MEMORYMASTER_WIKI_AUTOPROMOTE_THRESHOLD", "3")
    monkeypatch.setattr(
        lifecycle,
        "on_claim_confirmed",
        lambda claim_id_arg, db_path=None: calls.append((claim_id_arg, db_path)),
    )

    transition_claim(store, claim_id, "confirmed", reason="v1", event_type="validator")
    transition_claim(store, claim_id, "stale", reason="v2", event_type="validator")
    assert calls == []
    transition_claim(store, claim_id, "confirmed", reason="v3", event_type="validator")

    assert calls == [(claim_id, str(db))]


def test_lifecycle_autopromote_noop_without_hook(tmp_path: Path, monkeypatch) -> None:
    """With no hook registered the threshold crossing is a silent no-op —
    the transition itself must still land (autopromote is a side-channel)."""
    from memorymaster.core import lifecycle
    from memorymaster.core.lifecycle import transition_claim

    store, _, claim_id = _fresh_db(tmp_path)

    monkeypatch.setenv("MEMORYMASTER_WIKI_AUTOPROMOTE_THRESHOLD", "3")
    monkeypatch.setattr(lifecycle, "on_claim_confirmed", None)

    transition_claim(store, claim_id, "confirmed", reason="v1", event_type="validator")
    transition_claim(store, claim_id, "stale", reason="v2", event_type="validator")
    updated = transition_claim(store, claim_id, "confirmed", reason="v3", event_type="validator")

    assert updated.status == "confirmed"


def test_wiring_modules_register_autopromote_hook() -> None:
    """Importing service (or wiki_engine) must leave a registered hook behind —
    production paths (run_cycle, CLI, MCP, steward-cycle schtask) all import
    service, so autopromote keeps firing exactly as before the cut."""
    import memorymaster.core.service  # noqa: F401 — import is the wiring side effect
    from memorymaster.core import lifecycle

    assert lifecycle.on_claim_confirmed is not None


def test_auto_validate_uses_injected_store() -> None:
    """_auto_validate_claims must use the injected store and never build its
    own when one is supplied (the injection that replaced create_store)."""
    from memorymaster.govern.llm_steward import _auto_validate_claims

    class _RecordingStore:
        def __init__(self) -> None:
            self.requested: list[int] = []

        def get_claim(self, claim_id: int, include_citations: bool = False):
            self.requested.append(claim_id)
            return None  # no claims -> early return before run_deterministic

    fake = _RecordingStore()
    result = _auto_validate_claims("ignored.db", [11, 22], store=fake)

    assert fake.requested == [11, 22]
    assert result == {"checked": 0, "boosted": 0, "dropped": 0, "hard_conflicted": 0}
