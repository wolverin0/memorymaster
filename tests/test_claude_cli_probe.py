"""Capability-probed claude CLI resolver (plan 2.2).

WHY this matters: the LLM provider module deliberately returns "" on any
failure (graceful degradation — a dead LLM must never crash recall/steward).
But that means a `claude` binary which is present yet BROKEN (a stale/half-
upgraded install) fails every call and returns "", indistinguishable from a
model that legitimately produced no output — so the operator never learns the
CLI is dead and silently loses every extraction. The probe detects a broken
binary once, logs loudly, and exposes ``claude_cli_available()`` so the failure
is distinguishable from an empty success. These tests anchor on that
observability requirement, not on subprocess mechanics.

Borrowed from claude-mem's capability-probed binary resolver (re-survey 2026-06-24).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from memorymaster.core import llm_provider as lp


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    lp._CLAUDE_CLI_PROBE_CACHE.clear()
    yield
    lp._CLAUDE_CLI_PROBE_CACHE.clear()


def _fake_run(version_rc=0, version_exc=None, gen_stdout="OUTPUT", calls=None):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        if "--version" in cmd:
            if version_exc is not None:
                raise version_exc
            return SimpleNamespace(returncode=version_rc, stdout="claude x.y", stderr="")
        return SimpleNamespace(returncode=0, stdout=gen_stdout, stderr="")
    return run


def test_missing_binary_is_unavailable(monkeypatch):
    """No binary on PATH → unavailable (the clearest failure)."""
    monkeypatch.setattr(lp, "_resolve_claude_bin", lambda: None)
    assert lp.claude_cli_available() is False
    assert lp._call_claude_cli("p", "t") == ""


def test_broken_binary_is_unavailable(monkeypatch):
    """Present but `--version` exits non-zero → available() reports False, which
    is the signal that distinguishes a broken install from an empty success."""
    monkeypatch.setattr(lp, "_resolve_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(lp.subprocess, "run", _fake_run(version_rc=1))
    assert lp.claude_cli_available() is False


def test_unprobeable_binary_is_unavailable(monkeypatch):
    """`--version` raising OSError (e.g. not executable) → unavailable, not a crash."""
    monkeypatch.setattr(lp, "_resolve_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(lp.subprocess, "run", _fake_run(version_exc=OSError("denied")))
    assert lp.claude_cli_available() is False


def test_working_binary_generates(monkeypatch):
    """A binary that passes the probe is available and produces output."""
    monkeypatch.setattr(lp, "_resolve_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(lp.subprocess, "run", _fake_run(version_rc=0, gen_stdout="HELLO"))
    assert lp.claude_cli_available() is True
    assert lp._call_claude_cli("p", "t") == "HELLO"


def test_empty_success_is_distinguishable_from_failure(monkeypatch):
    """THE point of the guard: a working CLI returning empty output yields ""
    but reports available()==True, whereas a broken CLI also yields "" but
    available()==False. Same return value, different — now distinguishable — cause."""
    monkeypatch.setattr(lp, "_resolve_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(lp.subprocess, "run", _fake_run(version_rc=0, gen_stdout="   "))
    assert lp._call_claude_cli("p", "t") == ""      # empty (whitespace stripped)
    assert lp.claude_cli_available() is True          # ...but the CLI IS usable


def test_probe_is_cached_not_rerun_per_call(monkeypatch):
    """The probe must not pay a `--version` subprocess on every call — once per
    resolved binary, then cached (else it doubles cold-start latency)."""
    calls: list = []
    monkeypatch.setattr(lp, "_resolve_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(lp.subprocess, "run", _fake_run(version_rc=0, calls=calls))
    lp.claude_cli_available()
    lp.claude_cli_available()
    lp._call_claude_cli("p", "t")
    version_calls = [c for c in calls if "--version" in c]
    assert len(version_calls) == 1
