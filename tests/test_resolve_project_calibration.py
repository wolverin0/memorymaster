"""Threshold calibration harness (LOCALFS-SPEC.md §11).

Measures the auto-ingest threshold against a labelled roots directory instead
of guessing it.  Skipped in CI; run on the dev box with::

    MEMORYMASTER_CALIBRATE=1 \
    MEMORYMASTER_CALIBRATE_ROOT="G:/_OneDrive/OneDrive/Desktop/Py Apps" \
    python -m pytest tests/test_resolve_project_calibration.py -q

The harness code is always importable (so it cannot bit-rot); it only *runs*
when ``MEMORYMASTER_CALIBRATE=1``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from memorymaster.bridges.local_search.provider import PathHit
from memorymaster.bridges.local_search.resolver import resolve_project
from memorymaster.core.scope_utils import canonicalize_slug
from memorymaster.core.service import MemoryService

pytestmark = pytest.mark.calibration

_CALIBRATE_ENV = "MEMORYMASTER_CALIBRATE"
_ROOT_ENV = "MEMORYMASTER_CALIBRATE_ROOT"


class DirScanProvider:
    """Offline LocalSearchProvider that lists subdirectories of a root.

    Stands in for Everything when calibrating against a real projects folder
    without an ES.exe install.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def available(self) -> bool:
        return self._root.is_dir()

    def search(
        self, query: str, *, limit: int = 50, kind: str = "any"
    ) -> list[PathHit]:
        hits: list[PathHit] = []
        try:
            entries = list(self._root.iterdir())
        except OSError:
            return []
        for entry in entries:
            if not entry.is_dir():
                continue
            hits.append(
                PathHit(path=str(entry), kind="dir", size=None, modified=None)
            )
        return hits


def _build_labels(root: Path) -> dict[str, str]:
    """Map alias (dir basename) -> known-correct absolute path."""
    labels: dict[str, str] = {}
    for entry in root.iterdir():
        if entry.is_dir():
            labels[entry.name] = str(entry)
    return labels


def _sweep_thresholds(
    root: Path, svc: MemoryService
) -> list[tuple[float, int, int]]:
    """Return [(threshold, correct_auto_ingests, wrong_auto_ingests)]."""
    labels = _build_labels(root)
    provider = DirScanProvider(root)
    roots = [("projects", str(root))]

    # Record each alias's best match path + confidence once.
    observed: list[tuple[str, str | None, float]] = []
    for alias, correct_path in labels.items():
        result = resolve_project(
            alias,
            svc=svc,
            provider=provider,
            roots=roots,
            ingest_threshold=2.0,  # disable ingest during measurement
        )
        best = result.best
        observed.append(
            (
                correct_path,
                best.path if best else None,
                best.confidence if best else 0.0,
            )
        )

    rows: list[tuple[float, int, int]] = []
    threshold = 0.50
    while threshold <= 0.90 + 1e-9:
        correct = 0
        wrong = 0
        for correct_path, best_path, conf in observed:
            if best_path is None or conf < threshold:
                continue
            if canonicalize_slug(Path(best_path).name) == canonicalize_slug(
                Path(correct_path).name
            ):
                correct += 1
            else:
                wrong += 1
        rows.append((round(threshold, 2), correct, wrong))
        threshold += 0.05
    return rows


def test_calibration_sweep(tmp_path: Path) -> None:
    """Sweep thresholds; pick the lowest with zero wrong auto-ingests (<=0.85)."""
    if os.environ.get(_CALIBRATE_ENV) != "1":
        pytest.skip(f"set {_CALIBRATE_ENV}=1 to run the calibration harness")

    root_str = os.environ.get(_ROOT_ENV, "").strip()
    if not root_str:
        pytest.skip(f"set {_ROOT_ENV} to the labelled projects directory")
    root = Path(root_str)
    if not root.is_dir():
        pytest.skip(f"{_ROOT_ENV}={root_str!r} is not a directory")

    svc = MemoryService(tmp_path / "calib.db", workspace_root=tmp_path)
    svc.init_db()

    rows = _sweep_thresholds(root, svc)
    print("\nthreshold  correct  wrong")
    for threshold, correct, wrong in rows:
        print(f"{threshold:>9.2f}  {correct:>7d}  {wrong:>5d}")

    zero_wrong = [t for (t, _c, w) in rows if w == 0]
    assert zero_wrong, "no threshold achieved zero wrong auto-ingests"
    chosen = min(zero_wrong)
    print(f"\nchosen threshold (lowest with zero wrong): {chosen:.2f}")
    assert chosen <= 0.85
