"""T-2072: the full suite in scripts/bulk_check.py must run under a heavy slot (or an explicit opt-out)."""
from __future__ import annotations

import pytest

from scripts import bulk_check


@pytest.fixture
def ran(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(bulk_check, "_run", lambda name, argv: calls.append(name) or 0)
    monkeypatch.setattr(bulk_check.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.delenv(bulk_check.SLOT_MARKER_ENV, raising=False)
    monkeypatch.delenv(bulk_check.SLOT_OPT_OUT_ENV, raising=False)
    return calls


def test_direct_launch_with_workers_is_refused(ran, capsys):
    assert bulk_check.main(["--workers", "4"]) != 0
    assert ran == []  # nothing started, not even the fast gates
    err = capsys.readouterr().err.strip()
    assert len(err.splitlines()) == 1
    assert "heavy-slot.cjs" in err and bulk_check.SLOT_MARKER_ENV in err


def test_ml_launch_without_slot_is_refused(ran):
    assert bulk_check.main(["--ml"]) != 0
    assert ran == []


def test_marker_lets_the_suite_run(ran, monkeypatch):
    monkeypatch.setenv(bulk_check.SLOT_MARKER_ENV, "1")
    assert bulk_check.main([]) == 0
    assert "suite-not-ml" in ran


def test_opt_out_env_lets_the_suite_run(ran, monkeypatch):
    monkeypatch.setenv(bulk_check.SLOT_OPT_OUT_ENV, "1")
    assert bulk_check.main([]) == 0
    assert "suite-not-ml" in ran


def test_fast_needs_no_slot(ran):
    assert bulk_check.main(["--fast"]) == 0
    assert "suite-not-ml" not in ran and ran


def test_no_cli_flag_bypasses_the_guard(ran):
    with pytest.raises(SystemExit):
        bulk_check.main(["--no-slot"])
