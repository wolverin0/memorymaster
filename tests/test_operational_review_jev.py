"""Operational review: the ``jev_decisions`` check and the F-08 ``checkpoint_delivery`` check.

jev_decisions reads only the decisions ledger, physically read-only: FAIL when a
live hook surface (recall, session, hints) logged nothing in 24 h, when today's
spend exceeds the cap, or when a surface fell back on more than 30 % of at least 20
decisions that asked Jev in 24 h (``skip:`` rows are volume, not failures); WARN
when a live batch surface (revalidate, dedup, ingest) logged nothing in 36 h, when
live route (only while recall is off: the prompt hook skips S7 by design) or skills
logged nothing in 24 h, or when a question's weekly ECE rose by more than 0.05.
checkpoint_delivery
WARNs when the last successful checkpoint delivery (``orca-poke OK`` or the legacy
``poke-pane OK``) is older than 26 h.
"""
from __future__ import annotations

import hashlib
import runpy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.decisions.ledger import DecisionLedger, OutcomeRecord, utc_iso
from memorymaster.operations import operational_review as review

Q = runpy.run_path(str(Path(__file__).with_name("test_jev_review_queue.py")))
decision, item = Q["decision"], Q["item"]
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _jev_env(monkeypatch):
    for name in ("MEMORYMASTER_JEV_MODE", "MEMORYMASTER_JEV_DAILY_USD_CAP", "MEMORYMASTER_DECISIONS_DB",
                 "MEMORYMASTER_CHECKPOINT_LOG",
                 *(f"MEMORYMASTER_JEV_{s.upper()}" for s in ("revalidate", "recall", "ingest", "dedup", "skills",
                                                               "hints", "route", "session"))):
        monkeypatch.delenv(name, raising=False)


def _config(tmp_path, **kwargs):
    return review.ReviewConfig(db=tmp_path / "memory.db", decisions_db=tmp_path / "decisions.db",
                               checkpoint_log=tmp_path / "feature-checkpoint.log", **kwargs)


def _ledger(tmp_path):
    return DecisionLedger(tmp_path / "decisions.db")


def test_jev_off_without_ledger_passes_and_creates_nothing(tmp_path):
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.name == "jev_decisions" and result.verdict is review.Verdict.PASS
    assert not (tmp_path / "decisions.db").exists()


def test_live_surface_without_a_ledger_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_JEV_RECALL", "live")
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.FAIL and "recall" in result.detail
    assert not (tmp_path / "decisions.db").exists()


def test_live_surface_silent_for_24h_fails_and_active_one_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_JEV_MODE", "live")
    for surface in ("revalidate", "ingest", "dedup", "skills", "hints", "route", "session"):
        monkeypatch.setenv(f"MEMORYMASTER_JEV_{surface.upper()}", "off")
    ledger = _ledger(tmp_path)
    decision(ledger, "old", ts=NOW - timedelta(hours=30))
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.FAIL
    assert "silent_24h=recall" in result.detail
    decision(ledger, "fresh", ts=NOW - timedelta(hours=1))
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.PASS, result.detail
    assert result.counts["decisions_24h"] == 1


def _only_live(monkeypatch, *surfaces):
    monkeypatch.setenv("MEMORYMASTER_JEV_MODE", "off")
    for surface in surfaces:
        monkeypatch.setenv(f"MEMORYMASTER_JEV_{surface.upper()}", "live")


@pytest.mark.parametrize("surface", ["recall", "session", "hints"])
def test_a_silent_live_hook_surface_fails(tmp_path, monkeypatch, surface):
    """Hooks run on every prompt / session: a day without one decision means the hook is broken."""
    _only_live(monkeypatch, surface)
    ledger = _ledger(tmp_path)
    decision(ledger, "old", ts=NOW - timedelta(hours=25), surface=surface)
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.FAIL and f"silent_24h={surface}" in result.detail
    decision(ledger, "fresh", ts=NOW - timedelta(hours=2), surface=surface)
    assert review.check_jev_decisions(_config(tmp_path), now=NOW).verdict is review.Verdict.PASS


@pytest.mark.parametrize("surface", ["revalidate", "dedup", "ingest"])
def test_a_silent_batch_surface_only_warns_after_36h(tmp_path, monkeypatch, surface):
    """Steward cycles and Dreaming run in batches: a quiet day is normal, a day and a half is worth a look."""
    _only_live(monkeypatch, surface)
    ledger = _ledger(tmp_path)
    decision(ledger, "last_run", ts=NOW - timedelta(hours=30), surface=surface)
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.PASS, result.detail
    quiet = tmp_path / "quiet"
    quiet.mkdir()
    decision(_ledger(quiet), "long_ago", ts=NOW - timedelta(hours=40), surface=surface)
    result = review.check_jev_decisions(_config(quiet), now=NOW)
    assert result.verdict is review.Verdict.WARN and f"silent_36h={surface}" in result.detail
    assert result.counts["silent_live_surfaces"] == 0 and result.counts["quiet_live_surfaces"] == 1


def test_a_live_batch_surface_that_never_decided_warns_not_fails(tmp_path, monkeypatch):
    _only_live(monkeypatch, "revalidate", "recall")
    decision(_ledger(tmp_path), "fresh", ts=NOW - timedelta(hours=1))  # recall is alive
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.WARN and "silent_36h=revalidate" in result.detail


def test_only_batch_surfaces_live_without_a_ledger_warns(tmp_path, monkeypatch):
    _only_live(monkeypatch, "dedup")
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.WARN and "dedup" in result.detail
    assert not (tmp_path / "decisions.db").exists()


@pytest.mark.parametrize("recall_mode", ["live", "shadow"])
def test_route_is_exempt_from_silence_while_recall_is_not_off(tmp_path, monkeypatch, recall_mode):
    """S7 is skipped in the prompt hook by design while recall decides: no route rows is expected."""
    _only_live(monkeypatch, "route")
    monkeypatch.setenv("MEMORYMASTER_JEV_RECALL", recall_mode)
    decision(_ledger(tmp_path), "fresh", ts=NOW - timedelta(hours=1))
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.PASS, result.detail


def test_route_silent_while_recall_is_off_warns(tmp_path, monkeypatch):
    _only_live(monkeypatch, "route")
    decision(_ledger(tmp_path), "other", ts=NOW - timedelta(hours=1), surface="ingest")
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.WARN and "silent_24h=route" in result.detail


def test_silent_skills_warn_not_fail(tmp_path, monkeypatch):
    """S5 answers skill_recall on demand, not on every prompt: a quiet day is worth a look, not a failure."""
    _only_live(monkeypatch, "skills")
    decision(_ledger(tmp_path), "other", ts=NOW - timedelta(hours=1), surface="ingest")
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.WARN and "silent_24h=skills" in result.detail


def test_skip_rows_keep_a_surface_alive_and_are_not_fallbacks(tmp_path, monkeypatch):
    """``record_skip`` rows: the hook ran and chose not to ask Jev.  Volume, not failure."""
    _only_live(monkeypatch, "recall")
    ledger = _ledger(tmp_path)
    for index in range(25):
        decision(ledger, f"skip{index}", ts=NOW - timedelta(minutes=5 + index), fallback="skip:no_candidates",
                 outcome="not_sent", attempt=0, jev=None, cost=0.0)
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.PASS, result.detail
    assert result.counts["decisions_24h"] == 25 and result.counts["high_fallback_surfaces"] == 0
    for index in range(20):  # real failures still count, over the decisions that did ask
        decision(ledger, f"t{index}", ts=NOW - timedelta(minutes=40 + index), mode="shadow", fallback="timeout",
                 outcome="timeout", jev=None)
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.FAIL and "fallback" in result.detail and "20/20" in result.detail


def test_spend_today_over_the_cap_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_JEV_DAILY_USD_CAP", "0.05")
    ledger = _ledger(tmp_path)
    decision(ledger, "yesterday", ts=NOW - timedelta(hours=20), cost=0.04)  # previous UTC day
    decision(ledger, "a", ts=NOW - timedelta(hours=2), cost=0.03)
    assert review.check_jev_decisions(_config(tmp_path), now=NOW).verdict is review.Verdict.PASS
    decision(ledger, "b", ts=NOW - timedelta(hours=1), cost=0.03)
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.FAIL and "cost_today" in result.detail


@pytest.mark.parametrize("failures,total,verdict", [
    (7, 20, review.Verdict.FAIL),   # 35 % of 20
    (6, 20, review.Verdict.PASS),   # exactly 30 % is not "more than 30 %"
    (19, 19, review.Verdict.PASS),  # too few decisions to judge
])
def test_fallback_share_needs_more_than_30_percent_over_20_decisions(tmp_path, failures, total, verdict):
    ledger = _ledger(tmp_path)
    for index in range(total):
        failed = index < failures
        decision(ledger, f"d{index}", ts=NOW - timedelta(minutes=5 + index), mode="shadow" if failed else "live",
                 fallback="timeout" if failed else None, outcome="timeout" if failed else "ok")
    decision(ledger, "off", ts=NOW - timedelta(minutes=1), mode="off", fallback="mode_off", attempt=0)
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is verdict, result.detail
    if verdict is review.Verdict.FAIL:
        assert "fallback" in result.detail and "recall" in result.detail


def _calibration_week(ledger, prefix, start, *, used):
    for index in range(25):
        did = f"{prefix}{index}"
        decision(ledger, did, ts=start + timedelta(hours=index),
                 items=[item(did, f"claim:{index}", "recall.usable_evidence", 0.95)])
        if index < used:
            ledger.record_outcomes([OutcomeRecord(did, f"claim:{index}", "used_in_turn", 1.0, was_exposed=1,
                                                  label_source="detector",
                                                  observed_at=utc_iso(start + timedelta(hours=index, minutes=5)))])


def test_weekly_ece_rise_warns(tmp_path):
    ledger = _ledger(tmp_path)
    _calibration_week(ledger, "prev", NOW - timedelta(days=13), used=24)  # well calibrated at 0.95
    _calibration_week(ledger, "cur", NOW - timedelta(days=6), used=5)     # confident and mostly wrong
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.WARN
    assert "recall.usable_evidence@v1" in result.detail and "ece" in result.detail


def test_weekly_ece_rise_warns_for_outcomes_that_mature_after_7_days(tmp_path):
    """ingest.usefulness is graded by steward confirmation (7-day maturity): compare the two latest matured weeks."""
    ledger = _ledger(tmp_path)
    for prefix, start, confirmed in (("prev", NOW - timedelta(days=20), 24), ("cur", NOW - timedelta(days=13), 2)):
        for index in range(25):
            did = f"{prefix}{index}"
            decision(ledger, did, ts=start + timedelta(hours=index), surface="ingest", legacy='"admit"',
                     jev='"admit"', items=[item(did, f"cand:{prefix}{index}", "ingest.usefulness", 0.95)])
            if index < confirmed:
                ledger.record_outcomes([OutcomeRecord(did, f"cand:{prefix}{index}", "steward_confirmed", 1.0,
                                                      label_source="steward",
                                                      observed_at=utc_iso(start + timedelta(days=2)))])
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.WARN, result.detail
    assert "ingest.usefulness@v1" in result.detail and result.counts["ece_rises"] == 1


def test_small_weekly_samples_do_not_warn(tmp_path):
    ledger = _ledger(tmp_path)
    for index in range(5):
        did = f"x{index}"
        decision(ledger, did, ts=NOW - timedelta(days=3, hours=index),
                 items=[item(did, f"claim:{index}", "recall.usable_evidence", 0.95)])
    assert review.check_jev_decisions(_config(tmp_path), now=NOW).verdict is review.Verdict.PASS


def test_jev_check_never_writes_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_JEV_RECALL", "live")
    ledger = _ledger(tmp_path)
    decision(ledger, "fresh", ts=NOW - timedelta(hours=1))
    before = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert hashlib.sha256(ledger.path.read_bytes()).hexdigest() == before


def test_unreadable_ledger_fails_closed(tmp_path):
    (tmp_path / "decisions.db").write_text("not sqlite", encoding="utf-8")
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.FAIL and "probe_error" in result.detail


def test_unexpected_probe_error_is_one_fail_row_not_an_aborted_review(tmp_path, monkeypatch):
    """run_review has no per-check guard: an exception escaping this check would lose every other check's row."""
    from memorymaster.decisions import metrics

    def broken(*args, **kwargs):
        raise KeyError("schema drift")

    decision(_ledger(tmp_path), "fresh", ts=NOW - timedelta(hours=1))
    monkeypatch.setattr(metrics, "calibration_windows", broken)
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.FAIL and result.detail == "probe_error=KeyError"


def test_ledger_path_defaults_to_the_decisions_env(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere.db"
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(target))
    monkeypatch.setenv("MEMORYMASTER_JEV_RECALL", "live")
    decision(DecisionLedger(target), "fresh", ts=NOW - timedelta(hours=1))
    config = review.ReviewConfig(db=tmp_path / "memory.db")
    assert review.check_jev_decisions(config, now=NOW).verdict is review.Verdict.PASS


# --------------------------------------------------------- checkpoint (F-08) ---

def _log(tmp_path, *lines):
    path = tmp_path / "feature-checkpoint.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _stamp(delta):
    return (NOW - delta).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def test_recent_orca_delivery_passes(tmp_path):
    _log(tmp_path,
         f"{_stamp(timedelta(hours=50))} poke-pane FAIL(4): no pane matching project \"memorymaster\"",
         f"{_stamp(timedelta(hours=2))} orca-poke OK: 901 chars -> memorymaster")
    result = review.check_checkpoint_delivery(_config(tmp_path), now=NOW)
    assert result.name == "checkpoint_delivery" and result.verdict is review.Verdict.PASS
    assert "orca-poke" in result.detail
    assert result.counts["ok_lines"] == 1 and result.counts["fail_lines"] == 1


def test_legacy_poke_pane_ok_counts_as_delivery(tmp_path):
    _log(tmp_path, f"{_stamp(timedelta(hours=25))} poke-pane OK: 901 chars -> pane 12 (memorymaster)")
    assert review.check_checkpoint_delivery(_config(tmp_path), now=NOW).verdict is review.Verdict.PASS


def test_last_delivery_older_than_26h_warns_even_with_later_failures(tmp_path):
    _log(tmp_path,
         f"{_stamp(timedelta(hours=40))} poke-pane OK: 1003 chars -> pane 12",
         f"{_stamp(timedelta(hours=16))} poke-pane FAIL(3): wezterm unreachable after 3 attempts:")
    result = review.check_checkpoint_delivery(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.WARN and "40.0h" in result.detail


@pytest.mark.parametrize("lines", [
    (),
    ("2026-09-22T14:25:02.307Z poke-pane FAIL(3): wezterm unreachable",),
    ("garbage line", "2026-09-22T14:25:02Z orca-poke FAIL(1): no terminal"),
])
def test_no_successful_delivery_warns(tmp_path, lines):
    if lines:
        _log(tmp_path, *lines)
    assert review.check_checkpoint_delivery(_config(tmp_path), now=NOW).verdict is review.Verdict.WARN


def test_checkpoint_log_env_override(tmp_path, monkeypatch):
    path = tmp_path / "custom.log"
    path.write_text(f"{_stamp(timedelta(hours=1))} orca-poke OK: delivered\n", encoding="utf-8")
    monkeypatch.setenv("MEMORYMASTER_CHECKPOINT_LOG", str(path))
    config = review.ReviewConfig(db=tmp_path / "memory.db")
    assert review.check_checkpoint_delivery(config, now=NOW).verdict is review.Verdict.PASS


def test_run_review_and_cli_include_both_checks(tmp_path, monkeypatch, capsys):
    names = [check.__name__ for check in review.REVIEW_CHECKS]
    assert names[-2:] == ["check_jev_decisions", "check_checkpoint_delivery"]
    assert len(names) == 9
    captured = {}
    monkeypatch.setattr(review, "run_review", lambda config: captured.setdefault("config", config) and [])
    review.main(["--db", str(tmp_path / "memory.db"), "--decisions-db", str(tmp_path / "d.db"),
                 "--checkpoint-log", str(tmp_path / "c.log")])
    assert captured["config"].decisions_db == (tmp_path / "d.db").resolve()
    assert captured["config"].checkpoint_log == (tmp_path / "c.log").resolve()


def test_orphan_send_intents_count_as_spend_and_warn(tmp_path, monkeypatch):
    """A request whose decision row never landed (send intent only) is spend the
    operator must see: counted in cost_today and reported as a WARN."""
    monkeypatch.setenv("MEMORYMASTER_JEV_DAILY_USD_CAP", "0.05")
    ledger = _ledger(tmp_path)
    decision(ledger, "a", ts=NOW - timedelta(hours=2), cost=0.03)
    assert ledger.reserve_send("a", "recall", ts=utc_iso(NOW - timedelta(hours=2)), est_cost_usd=0.03)
    assert review.check_jev_decisions(_config(tmp_path), now=NOW).verdict is review.Verdict.PASS
    assert ledger.reserve_send("lost", "recall", ts=utc_iso(NOW - timedelta(hours=1)), est_cost_usd=0.001)
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.WARN and "orphan_send_intents_24h=1" in result.detail
    assert result.counts["orphan_send_intents_24h"] == 1
    assert ledger.reserve_send("lost2", "recall", ts=utc_iso(NOW - timedelta(minutes=30)), est_cost_usd=0.03)
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.verdict is review.Verdict.FAIL and "cost_today" in result.detail


def test_a_request_still_in_flight_is_not_an_orphan(tmp_path):
    """An intent written seconds ago has no decision row yet because its answer is
    still on the way (seen live); only intents older than a minute are orphans."""
    ledger = _ledger(tmp_path)
    assert ledger.reserve_send("inflight", "recall", ts=utc_iso(NOW - timedelta(seconds=10)), est_cost_usd=0.001)
    result = review.check_jev_decisions(_config(tmp_path), now=NOW)
    assert result.counts["orphan_send_intents_24h"] == 0
