"""Tests for the claude_cli LLM provider (memorymaster.llm_provider._call_claude_cli).

The provider shells out to the local `claude --print` binary. These tests mock
subprocess.run so they don't actually invoke the CLI — they cover the defensive
branches that the production user is most likely to hit:

  - missing binary on PATH
  - non-zero exit
  - timeout
  - UTF-8 round-trip including emoji
  - MEMORYMASTER_CLAUDE_CLI_BIN override
  - MEMORYMASTER_CLAUDE_CLI_TIMEOUT override
  - aliases ("claude_cli", "claude-cli") resolve to the same provider
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from memorymaster import llm_provider


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["claude", "--print", "--model", "x"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip any inherited claude_cli env vars so each test starts clean."""
    for key in (
        "MEMORYMASTER_CLAUDE_CLI_BIN",
        "MEMORYMASTER_CLAUDE_CLI_TIMEOUT",
        "MEMORYMASTER_LLM_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_missing_binary_returns_empty(monkeypatch, caplog):
    """When `claude` is not on PATH and no override is set, return '' and warn."""
    monkeypatch.setattr(llm_provider.shutil, "which", lambda _: None)
    with caplog.at_level("WARNING"):
        out = llm_provider._call_claude_cli("prompt", "text")
    assert out == ""
    assert any("binary not found" in rec.message for rec in caplog.records)


def test_non_zero_exit_returns_empty(monkeypatch, caplog):
    monkeypatch.setattr(llm_provider.shutil, "which", lambda _: "/fake/claude")
    fake_run = lambda *a, **kw: _completed(stdout="", stderr="boom", returncode=2)
    monkeypatch.setattr(llm_provider.subprocess, "run", fake_run)
    with caplog.at_level("WARNING"):
        out = llm_provider._call_claude_cli("prompt", "text")
    assert out == ""
    assert any("exit=2" in rec.message for rec in caplog.records)


def test_timeout_returns_empty(monkeypatch, caplog):
    monkeypatch.setattr(llm_provider.shutil, "which", lambda _: "/fake/claude")

    def _raise(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=120)

    monkeypatch.setattr(llm_provider.subprocess, "run", _raise)
    with caplog.at_level("WARNING"):
        out = llm_provider._call_claude_cli("prompt", "text")
    assert out == ""
    assert any("timed out" in rec.message for rec in caplog.records)


def test_oserror_returns_empty(monkeypatch, caplog):
    monkeypatch.setattr(llm_provider.shutil, "which", lambda _: "/fake/claude")

    def _raise(*_a, **_kw):
        raise OSError("permission denied")

    monkeypatch.setattr(llm_provider.subprocess, "run", _raise)
    with caplog.at_level("WARNING"):
        out = llm_provider._call_claude_cli("prompt", "text")
    assert out == ""
    assert any("subprocess failed" in rec.message for rec in caplog.records)


def test_utf8_emoji_roundtrip(monkeypatch):
    monkeypatch.setattr(llm_provider.shutil, "which", lambda _: "/fake/claude")
    captured = {}

    def _fake(args, input, capture_output, text, timeout, encoding, errors):
        captured["input"] = input
        captured["encoding"] = encoding
        return _completed(stdout="hola 👋 ñoño  \n", returncode=0)

    monkeypatch.setattr(llm_provider.subprocess, "run", _fake)
    out = llm_provider._call_claude_cli("Saludá", "ño 🌍 emoji")
    assert out == "hola 👋 ñoño"  # strip() applied
    assert captured["input"] == "Saludá\n\nño 🌍 emoji"
    assert captured["encoding"] == "utf-8"


def test_bin_override_honored(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_CLAUDE_CLI_BIN", "/custom/claude")
    # `which` should NOT be consulted when override is set.
    monkeypatch.setattr(
        llm_provider.shutil,
        "which",
        lambda _: pytest.fail("which should not be called when override set"),
    )
    captured = {}

    def _fake(args, **_kw):
        captured["argv"] = args
        return _completed(stdout="ok", returncode=0)

    monkeypatch.setattr(llm_provider.subprocess, "run", _fake)
    out = llm_provider._call_claude_cli("p", "t")
    assert out == "ok"
    assert captured["argv"][0] == "/custom/claude"


def test_timeout_override_honored(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_CLAUDE_CLI_TIMEOUT", "30")
    monkeypatch.setattr(llm_provider.shutil, "which", lambda _: "/fake/claude")
    captured = {}

    def _fake(args, **kw):
        captured["timeout"] = kw["timeout"]
        return _completed(stdout="ok", returncode=0)

    monkeypatch.setattr(llm_provider.subprocess, "run", _fake)
    llm_provider._call_claude_cli("p", "t")
    assert captured["timeout"] == 30


def test_model_override_honored(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_LLM_MODEL", "claude-sonnet-4-6-20251022")
    monkeypatch.setattr(llm_provider.shutil, "which", lambda _: "/fake/claude")
    captured = {}

    def _fake(args, **_kw):
        captured["argv"] = args
        return _completed(stdout="ok", returncode=0)

    monkeypatch.setattr(llm_provider.subprocess, "run", _fake)
    llm_provider._call_claude_cli("p", "t")
    assert "--model" in captured["argv"]
    model_idx = captured["argv"].index("--model") + 1
    assert captured["argv"][model_idx] == "claude-sonnet-4-6-20251022"


def test_default_model_is_haiku(monkeypatch):
    monkeypatch.setattr(llm_provider.shutil, "which", lambda _: "/fake/claude")
    captured = {}

    def _fake(args, **_kw):
        captured["argv"] = args
        return _completed(stdout="ok", returncode=0)

    monkeypatch.setattr(llm_provider.subprocess, "run", _fake)
    llm_provider._call_claude_cli("p", "t")
    model_idx = captured["argv"].index("--model") + 1
    assert captured["argv"][model_idx] == "claude-haiku-4-5-20251001"


def test_provider_aliases_register_same_function():
    """Both 'claude_cli' and 'claude-cli' must resolve to _call_claude_cli."""
    assert llm_provider._PROVIDERS["claude_cli"] is llm_provider._call_claude_cli
    assert llm_provider._PROVIDERS["claude-cli"] is llm_provider._call_claude_cli


def test_call_llm_dispatches_to_claude_cli(monkeypatch):
    """End-to-end: call_llm with provider=claude_cli should route to _call_claude_cli."""
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "claude_cli")
    monkeypatch.setattr(llm_provider.shutil, "which", lambda _: "/fake/claude")

    def _fake(args, **_kw):
        return _completed(stdout='[{"answer": 42}]', returncode=0)

    monkeypatch.setattr(llm_provider.subprocess, "run", _fake)
    out = llm_provider.call_llm("Q:", "What is the answer?")
    assert out == '[{"answer": 42}]'
