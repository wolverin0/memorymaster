"""The repository is public: the maintainer's home-lab addresses must not reach tracked files.

PR 164 scrubbed one such address from HEAD on 2026-06-22 without a guard, and the
same address came back in a 2026-09-29 operational-review note. Test fixtures keep
their own private addresses on purpose (they prove the sensitivity filter catches them).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME_LAB = re.compile(r"\b192\.168\.100\.\d{1,3}\b")
# Pre-existing defaults that predate this guard; each still needs its own fix.
KNOWN_DEBT = {"scripts/atlas_autoloop.py", "scripts/atlas_weekly_digest.py"}


def _tracked_text_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines() if not p.startswith("tests/") and p not in KNOWN_DEBT]


def offending_files(paths: list[str]) -> list[str]:
    hits = []
    for rel in paths:
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        if HOME_LAB.search(text):
            hits.append(rel)
    return hits


def test_tracked_files_do_not_publish_the_home_lab_subnet():
    assert offending_files(_tracked_text_files()) == []


def test_the_guard_catches_an_address_in_prose(tmp_path, monkeypatch):
    note = tmp_path / "note.md"
    note.write_text("server at 192.168.100.155:8765 was up", encoding="utf-8")
    monkeypatch.setattr(__import__(__name__), "ROOT", tmp_path)
    assert offending_files(["note.md"]) == ["note.md"]
