"""scripts/find_worktree_scopes.py is read-only and lists ids and scopes, never text (D-13)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

from memorymaster.core import scope_utils

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("find_worktree_scopes", ROOT / "scripts" / "find_worktree_scopes.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE claims (id INTEGER PRIMARY KEY, scope TEXT, status TEXT, text TEXT)")
    rows = [(1, "project:wt-copy", "confirmed"), (2, "project:wt-copy", "stale"), (3, "project:real", "confirmed"),
            (4, "project:linked", "confirmed"), (5, "user", "confirmed")]
    conn.executemany("INSERT INTO claims VALUES (?,?,?,'secret claim text')", rows)
    conn.commit()
    conn.close()


def test_lists_confirmed_claims_whose_scope_resolves_elsewhere_and_never_writes(tmp_path, monkeypatch, capsys):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"aliases": {"wt-copy": "project:real"}}), encoding="utf-8")
    monkeypatch.setenv(scope_utils.SCOPE_ALIASES_ENV, str(aliases))
    roots = tmp_path / "roots"
    (roots / "linked").mkdir(parents=True)  # a plain folder resolves to its own scope: no move
    db = tmp_path / "m.db"
    make_db(db)
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    assert tool.main(["--db", str(db), "--root", str(roots)]) == 0
    out = capsys.readouterr().out
    assert "1\tproject:wt-copy\tproject:real" in out and "claims_to_move=1" in out
    assert "secret" not in out and "\n2\t" not in out, "stale claims and claim text are never listed"
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before

    assert tool.main(["--db", str(db), "--counts"]) == 0
    assert "1\tproject:wt-copy\tproject:real" in capsys.readouterr().out
    assert tool.main(["--db", str(tmp_path / "missing.db")]) == 2
