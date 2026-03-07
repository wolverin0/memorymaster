from __future__ import annotations

from pathlib import Path

import pytest


_CASE_ROOT = Path(".tmp_cases")


def _prune_case_root(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.unlink()
        except OSError:
            continue
    for directory in sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            continue


@pytest.fixture(autouse=True)
def _cleanup_case_artifacts() -> None:
    _CASE_ROOT.mkdir(parents=True, exist_ok=True)
    _prune_case_root(_CASE_ROOT)
    yield
    _prune_case_root(_CASE_ROOT)
