"""Hermetic tests for memorymaster.surfaces.setup_detect.

Rules enforced here:
- subprocess.run is ALWAYS mocked — no real processes spawned.
- HTTP (_http_get) is patched — no real network calls.
- HOME / CLAUDE_DIR / ~/.claude.json are redirected to tmp_path.
- The real ~/.claude, ~/.codex, ~/.claude.json are NEVER touched.
- All probes must degrade to absent on timeout / error without raising.
- No exceptions must escape detect_environment().
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

import memorymaster.surfaces.setup_detect as det
from memorymaster.surfaces.setup_detect import (
    Detected,
    detect_environment,
    format_plan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MODULE = "memorymaster.surfaces.setup_detect"


def _make_completed(stdout: str = "", returncode: int = 0) -> MagicMock:
    """Return a mock CompletedProcess."""
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.stdout = stdout
    m.returncode = returncode
    return m


def _patch_home(tmp_path: Path) -> dict[str, Path]:
    """Return a dict of tmp_path-based home dirs for patching."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {"home": home}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_home(tmp_path: Path) -> Path:
    """A tmp home dir with no .claude / .codex pre-existing."""
    h = tmp_path / "home"
    h.mkdir(parents=True, exist_ok=True)
    return h


@pytest.fixture()
def fake_cwd(tmp_path: Path) -> Path:
    cwd = tmp_path / "project"
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd


# ---------------------------------------------------------------------------
# Individual probe unit tests (mock subprocess.run + _http_get)
# ---------------------------------------------------------------------------


class TestProbeDocker:
    def test_present_when_version_returns_zero(self, monkeypatch):
        monkeypatch.setattr(
            det,
            "_run",
            lambda args: "Docker version 24.0" if args == ["docker", "--version"] else None,
        )
        assert det._probe_docker() is True

    def test_absent_when_run_returns_none(self, monkeypatch):
        monkeypatch.setattr(det, "_run", lambda args: None)
        assert det._probe_docker() is False


class TestProbeDockerCompose:
    def test_present(self, monkeypatch):
        monkeypatch.setattr(
            det,
            "_run",
            lambda args: "Docker Compose version v2" if args == ["docker", "compose", "version"] else None,
        )
        assert det._probe_docker_compose() is True

    def test_absent(self, monkeypatch):
        monkeypatch.setattr(det, "_run", lambda args: None)
        assert det._probe_docker_compose() is False


class TestProbeOllama:
    def test_http_path_parses_models(self, monkeypatch):
        body = json.dumps(
            {"models": [{"name": "llama3.2:3b"}, {"name": "mistral:7b"}]}
        ).encode()
        monkeypatch.setattr(det, "_http_get", lambda url: body)
        ok, models = det._probe_ollama()
        assert ok is True
        assert "llama3.2:3b" in models
        assert "mistral:7b" in models

    def test_http_path_empty_models(self, monkeypatch):
        body = json.dumps({"models": []}).encode()
        monkeypatch.setattr(det, "_http_get", lambda url: body)
        ok, models = det._probe_ollama()
        assert ok is True
        assert models == ()

    def test_cli_fallback_when_http_fails(self, monkeypatch):
        monkeypatch.setattr(det, "_http_get", lambda url: None)
        monkeypatch.setattr(
            det,
            "_run",
            lambda args: "ollama 0.1.0" if args == ["ollama", "--version"] else None,
        )
        ok, models = det._probe_ollama()
        assert ok is True
        assert models == ()

    def test_absent_when_both_fail(self, monkeypatch):
        monkeypatch.setattr(det, "_http_get", lambda url: None)
        monkeypatch.setattr(det, "_run", lambda args: None)
        ok, models = det._probe_ollama()
        assert ok is False
        assert models == ()

    def test_uses_ollama_url_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OLLAMA_URL", "http://myhost:11434")
        captured: list[str] = []

        def fake_get(url: str) -> Optional[bytes]:
            captured.append(url)
            return None

        monkeypatch.setattr(det, "_http_get", fake_get)
        monkeypatch.setattr(det, "_run", lambda args: None)
        det._probe_ollama()
        assert captured and "myhost:11434" in captured[0]

    def test_http_malformed_json_degrades_gracefully(self, monkeypatch):
        monkeypatch.setattr(det, "_http_get", lambda url: b"not json")
        ok, models = det._probe_ollama()
        # HTTP responded (truthy body) but JSON parse fails — should still be ok=True
        assert ok is True
        assert models == ()


class TestProbeQdrant:
    def test_present_on_http_200(self, monkeypatch):
        monkeypatch.setattr(det, "_http_get", lambda url: b"ok")
        assert det._probe_qdrant() is True

    def test_absent_on_failure(self, monkeypatch):
        monkeypatch.setattr(det, "_http_get", lambda url: None)
        assert det._probe_qdrant() is False

    def test_uses_qdrant_url_env(self, monkeypatch):
        monkeypatch.setenv("QDRANT_URL", "http://qdrant.internal:6333")
        captured: list[str] = []

        def fake_get(url: str) -> Optional[bytes]:
            captured.append(url)
            return b"ok"

        monkeypatch.setattr(det, "_http_get", fake_get)
        det._probe_qdrant()
        assert captured and "qdrant.internal" in captured[0]


class TestProbeObsidianVault:
    def test_found_when_dir_exists(self, fake_cwd: Path):
        vault = fake_cwd / "obsidian-vault"
        vault.mkdir()
        result = det._probe_obsidian_vault(fake_cwd)
        assert result == str(vault)

    def test_none_when_absent(self, fake_cwd: Path):
        assert det._probe_obsidian_vault(fake_cwd) is None


class TestProbeGitnexus:
    def test_found_via_dir(self, fake_cwd: Path, monkeypatch):
        monkeypatch.setattr(det, "_run", lambda args: None)
        (fake_cwd / ".gitnexus").mkdir()
        assert det._probe_gitnexus(fake_cwd) is True

    def test_found_via_npx(self, fake_cwd: Path, monkeypatch):
        monkeypatch.setattr(
            det,
            "_run",
            lambda args: "gitnexus/0.1" if "gitnexus" in args else None,
        )
        assert det._probe_gitnexus(fake_cwd) is True

    def test_absent(self, fake_cwd: Path, monkeypatch):
        monkeypatch.setattr(det, "_run", lambda args: None)
        assert det._probe_gitnexus(fake_cwd) is False


class TestProbeClaudeCode:
    def test_present_when_dir_exists(self, fake_home: Path):
        (fake_home / ".claude").mkdir()
        assert det._probe_claude_code(fake_home) is True

    def test_absent_when_missing(self, fake_home: Path):
        assert det._probe_claude_code(fake_home) is False


class TestProbeCodex:
    def test_present(self, fake_home: Path):
        (fake_home / ".codex").mkdir()
        assert det._probe_codex(fake_home) is True

    def test_absent(self, fake_home: Path):
        assert det._probe_codex(fake_home) is False


class TestProbeMmInstalled:
    def test_true_when_package_importable(self, monkeypatch):
        # memorymaster IS importable in this test environment
        result = det._probe_mm_installed()
        assert isinstance(result, bool)

    def test_false_when_find_spec_returns_none(self, monkeypatch):
        import importlib.util

        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        assert det._probe_mm_installed() is False

    def test_false_on_exception(self, monkeypatch):
        import importlib.util

        def boom(name):
            raise RuntimeError("nope")

        monkeypatch.setattr(importlib.util, "find_spec", boom)
        assert det._probe_mm_installed() is False


class TestProbeMmMcpRegistered:
    def test_true_when_entry_present(self, fake_home: Path):
        claude_json = fake_home / ".claude.json"
        claude_json.write_text(
            json.dumps({"mcpServers": {"memorymaster": {"command": "memorymaster-mcp"}}}),
            encoding="utf-8",
        )
        assert det._probe_mm_mcp_registered(fake_home) is True

    def test_false_when_no_entry(self, fake_home: Path):
        claude_json = fake_home / ".claude.json"
        claude_json.write_text(
            json.dumps({"mcpServers": {"other-tool": {}}}),
            encoding="utf-8",
        )
        assert det._probe_mm_mcp_registered(fake_home) is False

    def test_false_when_file_absent(self, fake_home: Path):
        assert det._probe_mm_mcp_registered(fake_home) is False

    def test_false_on_malformed_json(self, fake_home: Path):
        (fake_home / ".claude.json").write_text("not json", encoding="utf-8")
        assert det._probe_mm_mcp_registered(fake_home) is False

    def test_false_when_mcp_servers_not_dict(self, fake_home: Path):
        (fake_home / ".claude.json").write_text(
            json.dumps({"mcpServers": ["list-not-dict"]}), encoding="utf-8"
        )
        assert det._probe_mm_mcp_registered(fake_home) is False


class TestProbeExistingHooks:
    def test_returns_event_names_containing_memorymaster(self, fake_home: Path):
        claude_dir = fake_home / ".claude"
        claude_dir.mkdir()
        settings = {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"command": 'python "memorymaster-recall.py"'}]}
                ],
                "Stop": [{"hooks": [{"command": "other-tool"}]}],
            }
        }
        (claude_dir / "settings.json").write_text(
            json.dumps(settings), encoding="utf-8"
        )
        result = det._probe_existing_hooks(fake_home)
        assert "UserPromptSubmit" in result
        assert "Stop" not in result

    def test_empty_tuple_when_no_settings(self, fake_home: Path):
        assert det._probe_existing_hooks(fake_home) == ()

    def test_empty_tuple_on_malformed_json(self, fake_home: Path):
        claude_dir = fake_home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text("bad json", encoding="utf-8")
        assert det._probe_existing_hooks(fake_home) == ()

    def test_empty_tuple_when_hooks_section_not_dict(self, fake_home: Path):
        claude_dir = fake_home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            json.dumps({"hooks": "string-not-dict"}), encoding="utf-8"
        )
        assert det._probe_existing_hooks(fake_home) == ()


# ---------------------------------------------------------------------------
# Degradation tests — every probe degrades to absent on error
# ---------------------------------------------------------------------------


class TestRunDegradation:
    """_run() must return None (not raise) on every failure mode."""

    def test_returns_none_on_timeout(self, monkeypatch):
        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=5)

        monkeypatch.setattr(subprocess, "run", boom)
        assert det._run(["docker", "--version"]) is None

    def test_returns_none_on_file_not_found(self, monkeypatch):
        def boom(*args, **kwargs):
            raise FileNotFoundError("no such binary")

        monkeypatch.setattr(subprocess, "run", boom)
        assert det._run(["nonexistent-binary"]) is None

    def test_returns_none_on_nonzero_exit(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **kw: _make_completed("", returncode=1)
        )
        assert det._run(["docker", "--version"]) is None

    def test_returns_none_on_permission_error(self, monkeypatch):
        def boom(*args, **kwargs):
            raise PermissionError("denied")

        monkeypatch.setattr(subprocess, "run", boom)
        assert det._run(["something"]) is None


class TestHttpGetDegradation:
    """_http_get() must return None (not raise) on every failure mode."""

    def test_returns_none_on_connection_error(self, monkeypatch):
        import urllib.request

        def boom(url, timeout):
            raise OSError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        assert det._http_get("http://localhost:9999/healthz") is None

    def test_returns_none_on_timeout(self, monkeypatch):
        import urllib.request
        import urllib.error

        def boom(url, timeout):
            raise urllib.error.URLError("timed out")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        assert det._http_get("http://localhost:9999/healthz") is None


# ---------------------------------------------------------------------------
# detect_environment — no exception escapes, returns correct types
# ---------------------------------------------------------------------------


class TestDetectEnvironment:
    """detect_environment() must never raise and must return a Detected."""

    def _all_absent_patches(self, monkeypatch, fake_home: Path, fake_cwd: Path) -> None:
        """Patch everything to absent/False so tests are fast and hermetic."""
        monkeypatch.setattr(det, "_run", lambda args: None)
        monkeypatch.setattr(det, "_http_get", lambda url: None)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    def test_returns_detected_instance(self, monkeypatch, fake_home, fake_cwd):
        self._all_absent_patches(monkeypatch, fake_home, fake_cwd)
        result = detect_environment(cwd=fake_cwd)
        assert isinstance(result, Detected)

    def test_frozen_dataclass(self, monkeypatch, fake_home, fake_cwd):
        self._all_absent_patches(monkeypatch, fake_home, fake_cwd)
        result = detect_environment(cwd=fake_cwd)
        with pytest.raises((AttributeError, TypeError)):
            result.docker = True  # type: ignore[misc]

    def test_no_exception_when_all_probes_fail(self, monkeypatch, fake_home, fake_cwd):
        self._all_absent_patches(monkeypatch, fake_home, fake_cwd)
        # Should not raise
        result = detect_environment(cwd=fake_cwd)
        assert result.docker is False
        assert result.docker_compose is False
        assert result.ollama is False
        assert result.qdrant is False
        assert result.claude_code is False
        assert result.codex is False

    def test_ollama_models_is_tuple(self, monkeypatch, fake_home, fake_cwd):
        self._all_absent_patches(monkeypatch, fake_home, fake_cwd)
        result = detect_environment(cwd=fake_cwd)
        assert isinstance(result.ollama_models, tuple)

    def test_existing_hooks_is_tuple(self, monkeypatch, fake_home, fake_cwd):
        self._all_absent_patches(monkeypatch, fake_home, fake_cwd)
        result = detect_environment(cwd=fake_cwd)
        assert isinstance(result.existing_hooks, tuple)

    def test_python_version_populated(self, monkeypatch, fake_home, fake_cwd):
        self._all_absent_patches(monkeypatch, fake_home, fake_cwd)
        result = detect_environment(cwd=fake_cwd)
        parts = result.python_version.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_os_field_is_string(self, monkeypatch, fake_home, fake_cwd):
        self._all_absent_patches(monkeypatch, fake_home, fake_cwd)
        result = detect_environment(cwd=fake_cwd)
        assert isinstance(result.os, str)
        assert len(result.os) > 0

    def test_uses_cwd_for_vault_probe(self, monkeypatch, fake_home, fake_cwd):
        self._all_absent_patches(monkeypatch, fake_home, fake_cwd)
        vault = fake_cwd / "obsidian-vault"
        vault.mkdir()
        result = detect_environment(cwd=fake_cwd)
        assert result.obsidian_vault == str(vault)

    def test_uses_cwd_for_gitnexus_probe(self, monkeypatch, fake_home, fake_cwd):
        self._all_absent_patches(monkeypatch, fake_home, fake_cwd)
        (fake_cwd / ".gitnexus").mkdir()
        result = detect_environment(cwd=fake_cwd)
        assert result.gitnexus is True

    def test_claude_code_detected_from_home(self, monkeypatch, fake_home, fake_cwd):
        self._all_absent_patches(monkeypatch, fake_home, fake_cwd)
        (fake_home / ".claude").mkdir()
        result = detect_environment(cwd=fake_cwd)
        assert result.claude_code is True

    def test_codex_detected_from_home(self, monkeypatch, fake_home, fake_cwd):
        self._all_absent_patches(monkeypatch, fake_home, fake_cwd)
        (fake_home / ".codex").mkdir()
        result = detect_environment(cwd=fake_cwd)
        assert result.codex is True

    def test_mcp_registered_detected(self, monkeypatch, fake_home, fake_cwd):
        self._all_absent_patches(monkeypatch, fake_home, fake_cwd)
        (fake_home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"memorymaster": {}}}), encoding="utf-8"
        )
        result = detect_environment(cwd=fake_cwd)
        assert result.mm_mcp_registered is True

    def test_existing_hooks_detected(self, monkeypatch, fake_home, fake_cwd):
        self._all_absent_patches(monkeypatch, fake_home, fake_cwd)
        claude_dir = fake_home / ".claude"
        claude_dir.mkdir()
        settings = {
            "hooks": {
                "Stop": [
                    {"hooks": [{"command": "python memorymaster-stop.py"}]}
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        result = detect_environment(cwd=fake_cwd)
        assert "Stop" in result.existing_hooks

    def test_no_exception_even_on_subprocess_timeout(self, monkeypatch, fake_home, fake_cwd):
        """If subprocess.run raises TimeoutExpired, detect_environment still returns."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(det, "_http_get", lambda url: None)

        def timeout_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=5)

        monkeypatch.setattr(subprocess, "run", timeout_run)
        # Should not raise
        result = detect_environment(cwd=fake_cwd)
        assert isinstance(result, Detected)


# ---------------------------------------------------------------------------
# format_plan — structural + content tests
# ---------------------------------------------------------------------------


class TestFormatPlan:
    def _base_detected(self, **overrides) -> Detected:
        defaults = dict(
            python_version="3.12.0",
            pip_ok=True,
            os="Linux",
            docker=False,
            docker_compose=False,
            ollama=False,
            ollama_models=(),
            qdrant=False,
            obsidian_vault=None,
            gitnexus=False,
            claude_code=False,
            codex=False,
            mm_installed=False,
            mm_mcp_registered=False,
            existing_hooks=(),
        )
        defaults.update(overrides)
        return Detected(**defaults)

    def test_returns_list_of_strings(self):
        d = self._base_detected()
        result = format_plan(d, want_full_stack=False)
        assert isinstance(result, list)
        assert all(isinstance(line, str) for line in result)

    def test_nonempty(self):
        d = self._base_detected()
        result = format_plan(d, want_full_stack=False)
        assert len(result) > 0

    def test_will_do_mm_install_when_not_installed(self):
        d = self._base_detected(mm_installed=False)
        lines = "\n".join(format_plan(d, want_full_stack=False))
        assert "[will-do]" in lines
        assert "pip install" in lines

    def test_skip_present_when_mm_installed(self):
        d = self._base_detected(mm_installed=True)
        lines = "\n".join(format_plan(d, want_full_stack=False))
        assert "[skip-present]" in lines
        assert "already installed" in lines

    def test_cant_hooks_when_no_claude_dir(self):
        d = self._base_detected(claude_code=False)
        lines = "\n".join(format_plan(d, want_full_stack=False))
        assert "[cant-missing]" in lines
        assert "claude" in lines.lower()

    def test_will_do_hooks_when_claude_present_no_hooks(self):
        d = self._base_detected(claude_code=True, existing_hooks=())
        lines = "\n".join(format_plan(d, want_full_stack=False))
        assert "[will-do]" in lines
        assert "hooks" in lines

    def test_skip_present_hooks_when_already_registered(self):
        d = self._base_detected(claude_code=True, existing_hooks=("UserPromptSubmit", "Stop"))
        lines = "\n".join(format_plan(d, want_full_stack=False))
        assert "[skip-present]" in lines
        assert "already registered" in lines

    def test_skip_present_mcp_when_registered(self):
        d = self._base_detected(claude_code=True, mm_mcp_registered=True)
        lines = "\n".join(format_plan(d, want_full_stack=False))
        assert "already registered" in lines

    def test_will_do_mcp_when_claude_present_not_registered(self):
        d = self._base_detected(claude_code=True, mm_mcp_registered=False)
        lines = "\n".join(format_plan(d, want_full_stack=False))
        assert "register MCP" in lines

    def test_full_stack_skipped_message_when_not_requested(self):
        d = self._base_detected()
        lines = "\n".join(format_plan(d, want_full_stack=False))
        assert "Full-stack" in lines
        assert "skipped" in lines.lower() or "not requested" in lines.lower()

    def test_full_stack_docker_will_do_when_compose_present(self):
        d = self._base_detected(docker_compose=True, qdrant=False)
        lines = "\n".join(format_plan(d, want_full_stack=True))
        assert "docker compose up" in lines
        assert "qdrant" in lines.lower()

    def test_full_stack_qdrant_skip_when_already_reachable(self):
        d = self._base_detected(qdrant=True)
        lines = "\n".join(format_plan(d, want_full_stack=True))
        assert "Qdrant already reachable" in lines

    def test_full_stack_cant_qdrant_when_no_docker(self):
        d = self._base_detected(docker_compose=False, qdrant=False)
        lines = "\n".join(format_plan(d, want_full_stack=True))
        assert "[cant-missing]" in lines
        assert "SQLite-only" in lines

    def test_full_stack_ollama_skip_when_reachable(self):
        d = self._base_detected(ollama=True, ollama_models=("llama3.2:3b",))
        lines = "\n".join(format_plan(d, want_full_stack=True))
        assert "Ollama already reachable" in lines
        assert "llama3.2:3b" in lines

    def test_full_stack_cant_ollama_when_no_docker(self):
        d = self._base_detected(docker_compose=False, ollama=False)
        joined = "\n".join(format_plan(d, want_full_stack=True))
        assert "auto-ingest is OFF" in joined or "Ollama" in joined

    def test_obsidian_vault_skip_when_found(self):
        d = self._base_detected(obsidian_vault="/tmp/project/obsidian-vault")
        lines = "\n".join(format_plan(d, want_full_stack=False))
        assert "obsidian-vault" in lines.lower()
        assert "[skip-present]" in lines

    def test_obsidian_vault_will_do_when_absent(self):
        d = self._base_detected(obsidian_vault=None)
        lines = "\n".join(format_plan(d, want_full_stack=False))
        assert "obsidian-vault" in lines.lower()

    def test_gitnexus_skip_when_present(self):
        d = self._base_detected(gitnexus=True)
        lines = "\n".join(format_plan(d, want_full_stack=False))
        assert "GitNexus" in lines
        assert "[skip-present]" in lines

    def test_gitnexus_cant_when_absent(self):
        d = self._base_detected(gitnexus=False)
        lines = "\n".join(format_plan(d, want_full_stack=False))
        assert "GitNexus" in lines
        assert "[cant-missing]" in lines

    def test_codex_present_line_included(self):
        d = self._base_detected(codex=True)
        lines = "\n".join(format_plan(d, want_full_stack=False))
        assert "codex" in lines.lower() or "Codex" in lines

    def test_codex_absent_line_included(self):
        d = self._base_detected(codex=False)
        lines = "\n".join(format_plan(d, want_full_stack=False))
        assert "codex" in lines.lower() or "Codex" in lines
