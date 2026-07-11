"""Hermetic tests for memorymaster.surfaces.setup_hooks.

SAFETY (mandatory): every test patches the module-level HOME / CLAUDE_DIR /
CLAUDE_JSON / CODEX_DIR to ``tmp_path`` and mocks ``subprocess.run`` so the
REAL ``~/.claude`` / ``~/.codex`` is NEVER touched and the REAL installer is
NEVER executed against this machine. detect_environment is also stubbed so no
real Docker/Ollama/Qdrant probes run.

Coverage (spec §5):
- non-interactive ``--yes`` wires hooks/MCP into a tmp HOME
- re-run is idempotent (no duplicate hook entries; settings.json valid JSON)
- MCP registration uses the non-deprecated command
- no-Docker fallback: setup succeeds, degraded message emitted, exit 0
- verify_install round-trips a sentinel claim on a tmp DB (PASS)
- ``--json`` emits valid parseable JSON
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import memorymaster.surfaces.setup_hooks as sh
from memorymaster.surfaces.setup_detect import Detected


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _detected(**overrides) -> Detected:
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
        claude_code=True,
        codex=False,
        mm_installed=True,
        mm_mcp_registered=False,
        existing_hooks=(),
    )
    defaults.update(overrides)
    return Detected(**defaults)


@pytest.fixture()
def hermetic_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect every filesystem target into tmp_path. The real HOME is safe."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    codex_dir = home / ".codex"
    claude_json = home / ".claude.json"
    claude_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(sh, "HOME", home)
    monkeypatch.setattr(sh, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(sh, "CLAUDE_JSON", claude_json)
    monkeypatch.setattr(sh, "CODEX_DIR", codex_dir)

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sh, "PROJECT_ROOT", project)

    return {
        "home": home,
        "claude_dir": claude_dir,
        "codex_dir": codex_dir,
        "claude_json": claude_json,
        "project": project,
    }


@pytest.fixture()
def no_real_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record subprocess.run calls and return a benign success — never spawn."""
    calls: list[list[str]] = []

    def fake_run(args, *a, **kw):
        calls.append(list(args) if isinstance(args, (list, tuple)) else [args])
        m = subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return m

    monkeypatch.setattr(sh.subprocess, "run", fake_run)
    return calls


def _stub_detect(monkeypatch: pytest.MonkeyPatch, detected: Detected) -> None:
    monkeypatch.setattr(sh, "detect_environment", lambda *a, **kw: detected)


# ---------------------------------------------------------------------------
# ask / ask_yn non-interactive honoring
# ---------------------------------------------------------------------------


class TestNonInteractive:
    def test_ask_returns_default_when_non_interactive(self, monkeypatch):
        sh.set_non_interactive(True)
        try:
            # input() must NOT be called — if it is, this raises.
            monkeypatch.setattr("builtins.input", lambda *_: (_ for _ in ()).throw(AssertionError("input called")))
            assert sh.ask("anything", "the-default") == "the-default"
            assert sh.ask_yn("ok?", True) is True
            assert sh.ask_yn("ok?", False) is False
        finally:
            sh.set_non_interactive(False)

    def test_ask_uses_input_when_interactive(self, monkeypatch):
        sh.set_non_interactive(False)
        monkeypatch.setattr("builtins.input", lambda *_: "typed-value")
        assert sh.ask("q", "def") == "typed-value"


# ---------------------------------------------------------------------------
# MCP registration — non-deprecated command + brownfield idempotency
# ---------------------------------------------------------------------------


class TestInstallMcp:
    def test_writes_explicit_local_trusted_auth_mode(self, hermetic_home):
        sh.install_mcp(force=True)
        data = json.loads(hermetic_home["claude_json"].read_text(encoding="utf-8"))
        entry = data["mcpServers"]["memorymaster"]
        assert entry["env"]["MEMORYMASTER_MCP_AUTH_MODE"] == "local-trusted"

    def test_uses_non_deprecated_command(self, hermetic_home):
        sh.install_mcp(force=True)
        data = json.loads(hermetic_home["claude_json"].read_text(encoding="utf-8"))
        entry = data["mcpServers"]["memorymaster"]
        assert entry["args"] == ["-m", "memorymaster.surfaces.mcp_server"]
        # Must NOT be the deprecated path.
        assert "memorymaster.mcp_server" not in json.dumps(entry["args"])

    def test_skips_existing_without_force(self, hermetic_home):
        sh.set_non_interactive(True)  # ask_yn returns its default (False here)
        try:
            hermetic_home["claude_json"].write_text(
                json.dumps({"mcpServers": {"memorymaster": {"sentinel": "keep-me"}}}),
                encoding="utf-8",
            )
            sh.install_mcp(force=False)
            data = json.loads(hermetic_home["claude_json"].read_text(encoding="utf-8"))
            # Brownfield: untouched.
            assert data["mcpServers"]["memorymaster"] == {"sentinel": "keep-me"}
        finally:
            sh.set_non_interactive(False)

    def test_force_overwrites(self, hermetic_home):
        hermetic_home["claude_json"].write_text(
            json.dumps({"mcpServers": {"memorymaster": {"old": True}}, "keepKey": 1}),
            encoding="utf-8",
        )
        sh.install_mcp(force=True)
        data = json.loads(hermetic_home["claude_json"].read_text(encoding="utf-8"))
        assert data["mcpServers"]["memorymaster"]["args"] == ["-m", "memorymaster.surfaces.mcp_server"]
        # Unknown keys preserved.
        assert data["keepKey"] == 1

    def test_preserves_other_servers(self, hermetic_home):
        hermetic_home["claude_json"].write_text(
            json.dumps({"mcpServers": {"other": {"x": 1}}}), encoding="utf-8"
        )
        sh.install_mcp(force=True)
        data = json.loads(hermetic_home["claude_json"].read_text(encoding="utf-8"))
        assert data["mcpServers"]["other"] == {"x": 1}
        assert "memorymaster" in data["mcpServers"]


class TestInstallMcpCodex:
    def test_writes_explicit_local_trusted_auth_mode(self, hermetic_home):
        hermetic_home["codex_dir"].mkdir(parents=True, exist_ok=True)
        sh.install_mcp_codex(force=True)
        content = (hermetic_home["codex_dir"] / "config.toml").read_text(encoding="utf-8")
        assert 'MEMORYMASTER_MCP_AUTH_MODE = "local-trusted"' in content

    def test_writes_managed_block_with_correct_command(self, hermetic_home):
        hermetic_home["codex_dir"].mkdir(parents=True, exist_ok=True)
        sh.install_mcp_codex(force=True)
        content = (hermetic_home["codex_dir"] / "config.toml").read_text(encoding="utf-8")
        assert "[mcp_servers.memorymaster]" in content
        assert "memorymaster.surfaces.mcp_server" in content
        assert sh._CODEX_MCP_BEGIN in content and sh._CODEX_MCP_END in content

    def test_idempotent_block_no_duplication(self, hermetic_home):
        hermetic_home["codex_dir"].mkdir(parents=True, exist_ok=True)
        sh.install_mcp_codex(force=True)
        sh.install_mcp_codex(force=True)
        content = (hermetic_home["codex_dir"] / "config.toml").read_text(encoding="utf-8")
        assert content.count("[mcp_servers.memorymaster]") == 1

    def test_preserves_unmanaged_toml(self, hermetic_home):
        hermetic_home["codex_dir"].mkdir(parents=True, exist_ok=True)
        cfg = hermetic_home["codex_dir"] / "config.toml"
        cfg.write_text('model = "gpt-5"\n', encoding="utf-8")
        sh.install_mcp_codex(force=True)
        content = cfg.read_text(encoding="utf-8")
        assert 'model = "gpt-5"' in content
        assert "[mcp_servers.memorymaster]" in content

    def test_noop_when_no_codex_dir(self, hermetic_home):
        # codex_dir does not exist → no file created
        sh.install_mcp_codex(force=True)
        assert not (hermetic_home["codex_dir"] / "config.toml").exists()


# ---------------------------------------------------------------------------
# install_hooks idempotency
# ---------------------------------------------------------------------------


class TestInstallHooksIdempotent:
    def test_no_duplicate_hooks_on_rerun(self, hermetic_home, monkeypatch):
        # config_templates/hooks must exist as package resource; stub the copy
        # loop is unnecessary — install_hooks reads real templates and writes
        # into the tmp CLAUDE_DIR. We only need settings.json behavior.
        llm = {"provider": "ollama", "api_key": "", "model": "llama3.2:3b"}
        sh.install_hooks(llm)
        sh.install_hooks(llm)
        settings = json.loads(
            (hermetic_home["claude_dir"] / "settings.json").read_text(encoding="utf-8")
        )
        ups = settings["hooks"]["UserPromptSubmit"]
        mm_entries = [h for h in ups if "memorymaster" in json.dumps(h)]
        # recall + classify = exactly 2, not 4.
        assert len(mm_entries) == 2

    def test_settings_json_stays_valid(self, hermetic_home):
        llm = {"provider": "ollama", "api_key": "", "model": "llama3.2:3b"}
        sh.install_hooks(llm)
        # Must parse without error.
        json.loads((hermetic_home["claude_dir"] / "settings.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Full-stack: no-Docker fallback
# ---------------------------------------------------------------------------


class TestSetupFullStack:
    def test_no_docker_fallback_degraded(self, hermetic_home, no_real_subprocess):
        det = _detected(docker_compose=False, qdrant=False, ollama=False)
        result = sh.setup_full_stack(det, interactive=False, yes=True)
        assert result["degraded"] is True
        assert "SQLite-only" in result["message"]
        # No docker compose invoked.
        assert not any("compose" in c for call in no_real_subprocess for c in call)

    def test_reuses_already_healthy(self, hermetic_home, no_real_subprocess):
        det = _detected(qdrant=True, ollama=True)
        result = sh.setup_full_stack(det, interactive=False, yes=True)
        assert result["degraded"] is False
        assert result["qdrant"] == "reused"
        assert result["ollama"] == "reused"

    def test_compose_up_when_available(self, hermetic_home, no_real_subprocess):
        det = _detected(docker_compose=True, qdrant=False, ollama=False)
        result = sh.setup_full_stack(det, interactive=False, yes=True, model="llama3.2:3b")
        assert result["compose_run"] is True
        assert result["degraded"] is False
        assert any("compose" in c for call in no_real_subprocess for c in call)

    def test_compose_failure_degrades_not_raises(self, hermetic_home, monkeypatch):
        def boom(args, *a, **kw):
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="nope")

        monkeypatch.setattr(sh.subprocess, "run", boom)
        det = _detected(docker_compose=True, qdrant=False, ollama=False)
        result = sh.setup_full_stack(det, interactive=False, yes=True)
        assert result["degraded"] is True


# ---------------------------------------------------------------------------
# verify_install — round-trip on a tmp DB
# ---------------------------------------------------------------------------


class TestVerifyInstall:
    def test_round_trip_pass(self, tmp_path, hermetic_home, monkeypatch):
        # MCP not registered → no mcp_note required; keep detect stub absent.
        monkeypatch.setattr(sh, "detect_environment", lambda *a, **kw: _detected(mm_mcp_registered=False))
        db = tmp_path / "verify.db"
        result = sh.verify_install(db)
        assert result["status"] == "PASS", result
        assert "sentinel" in result["detail"]

    def test_mcp_note_when_registered(self, tmp_path, hermetic_home, monkeypatch):
        monkeypatch.setattr(sh, "detect_environment", lambda *a, **kw: _detected(mm_mcp_registered=True))
        db = tmp_path / "verify2.db"
        result = sh.verify_install(db)
        assert result["status"] == "PASS"
        assert "restart" in result["mcp_note"].lower()


# ---------------------------------------------------------------------------
# main() end-to-end (non-interactive) — wiring + idempotency + JSON + exit 0
# ---------------------------------------------------------------------------


def _run_main(monkeypatch, hermetic_home, det, argv):
    """Run main() fully hermetic: detect stubbed, subprocess mocked."""
    _stub_detect(monkeypatch, det)

    def fake_run(args, *a, **kw):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sh.subprocess, "run", fake_run)
    # main resets PROJECT_ROOT from --project-root; point it at the tmp project.
    return sh.main(argv)


class TestMainNonInteractive:
    def _argv(self, hermetic_home, extra=None):
        argv = [
            "--yes",
            "--provider",
            "ollama",
            "--project-root",
            str(hermetic_home["project"]),
            "--db",
            str(hermetic_home["project"] / "memorymaster.db"),
            "--no-cron",
            "--no-obsidian-skills",
            "--no-full-stack",
        ]
        if extra:
            argv += extra
        return argv

    def test_exit_zero_and_wires_hooks_mcp(self, monkeypatch, hermetic_home, capsys):
        det = _detected(claude_code=True, mm_mcp_registered=False)
        rc = _run_main(monkeypatch, hermetic_home, det, self._argv(hermetic_home))
        assert rc == 0
        # settings.json written into tmp HOME, valid JSON.
        settings = json.loads(
            (hermetic_home["claude_dir"] / "settings.json").read_text(encoding="utf-8")
        )
        assert "UserPromptSubmit" in settings["hooks"]
        # MCP registered with correct command.
        data = json.loads(hermetic_home["claude_json"].read_text(encoding="utf-8"))
        assert data["mcpServers"]["memorymaster"]["args"] == ["-m", "memorymaster.surfaces.mcp_server"]

    def test_rerun_idempotent(self, monkeypatch, hermetic_home):
        det1 = _detected(claude_code=True, mm_mcp_registered=False)
        _run_main(monkeypatch, hermetic_home, det1, self._argv(hermetic_home))
        first = (hermetic_home["claude_dir"] / "settings.json").read_text(encoding="utf-8")

        # Second run: detection now reports hooks + MCP already present.
        det2 = _detected(
            claude_code=True,
            mm_mcp_registered=True,
            existing_hooks=("UserPromptSubmit", "Stop"),
        )
        _run_main(monkeypatch, hermetic_home, det2, self._argv(hermetic_home))
        second = (hermetic_home["claude_dir"] / "settings.json").read_text(encoding="utf-8")

        s2 = json.loads(second)
        # No duplicate memorymaster hooks accumulated.
        ups = s2["hooks"]["UserPromptSubmit"]
        assert len([h for h in ups if "memorymaster" in json.dumps(h)]) == 2
        # MCP entry not clobbered (idempotent) — still the correct command.
        data = json.loads(hermetic_home["claude_json"].read_text(encoding="utf-8"))
        assert data["mcpServers"]["memorymaster"]["args"] == ["-m", "memorymaster.surfaces.mcp_server"]
        assert json.loads(first) and json.loads(second)  # both valid JSON

    def test_no_docker_fallback_exits_zero(self, monkeypatch, hermetic_home, capsys):
        det = _detected(docker_compose=False, qdrant=False, ollama=False)
        argv = [
            "--yes",
            "--provider",
            "ollama",
            "--project-root",
            str(hermetic_home["project"]),
            "--db",
            str(hermetic_home["project"] / "memorymaster.db"),
            "--no-cron",
            "--no-obsidian-skills",
            "--full-stack",
            "--json",
        ]
        rc = _run_main(monkeypatch, hermetic_home, det, argv)
        assert rc == 0
        out = capsys.readouterr().out
        # JSON must be parseable and report degraded.
        payload = json.loads(out)
        assert payload["degraded"] is True

    def test_json_output_parses(self, monkeypatch, hermetic_home, capsys):
        det = _detected(claude_code=True)
        rc = _run_main(
            monkeypatch, hermetic_home, det, self._argv(hermetic_home, extra=["--json"])
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert set(["detected", "planned", "applied", "verify", "degraded"]).issubset(payload)
        assert payload["verify"]["status"] in ("PASS", "PARTIAL")


class TestVerifyOnly:
    def test_verify_only_short_circuits(self, monkeypatch, hermetic_home, capsys):
        monkeypatch.setattr(sh, "detect_environment", lambda *a, **kw: _detected(mm_mcp_registered=False))
        db = hermetic_home["project"] / "vo.db"
        rc = sh.main(
            [
                "--verify-only",
                "--project-root",
                str(hermetic_home["project"]),
                "--db",
                str(db),
                "--json",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["verify"]["status"] == "PASS"


class TestMalformedConfigBackup:
    """A malformed pre-existing config must be BACKED UP, never silently wiped.

    WHY: ~/.claude/settings.json and ~/.claude.json are hand-edited daily-driver
    configs. The pre-fix code reset malformed files to {} and overwrote them,
    losing user data. These tests fail if that data-loss regression returns.
    """

    def test_malformed_settings_json_is_backed_up(self, hermetic_home, no_real_subprocess):
        settings = hermetic_home["claude_dir"] / "settings.json"
        settings.write_text('{ this is : not valid json,,, ', encoding="utf-8")
        original = settings.read_text(encoding="utf-8")

        sh.install_hooks({"provider": "ollama", "api_key": "", "model": "llama3.2:3b"})

        # settings.json is now valid JSON with our hooks wired in...
        rewritten = json.loads(settings.read_text(encoding="utf-8"))
        assert "memorymaster" in json.dumps(rewritten["hooks"])
        # ...and the original malformed content was preserved in a .corrupt-*.bak
        backups = list(hermetic_home["claude_dir"].glob("settings.json.corrupt-*.bak"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == original

    def test_malformed_claude_json_is_backed_up(self, hermetic_home, no_real_subprocess):
        sh.set_non_interactive(True)
        try:
            cj = hermetic_home["claude_json"]
            cj.write_text('{"mcpServers": BROKEN', encoding="utf-8")
            original = cj.read_text(encoding="utf-8")

            sh.install_mcp(force=False)

            data = json.loads(cj.read_text(encoding="utf-8"))
            assert "memorymaster" in data["mcpServers"]
            backups = list(hermetic_home["home"].glob(".claude.json.corrupt-*.bak"))
            assert len(backups) == 1
            assert backups[0].read_text(encoding="utf-8") == original
        finally:
            sh.set_non_interactive(False)


class TestFromZeroRegressions:
    """Regressions found by a real from-zero container install (not catchable by
    the stub-detect/populated-HOME unit harness). Each fails if its bug returns.
    """

    def test_no_claude_code_completes_without_crash(self, hermetic_home, no_real_subprocess, monkeypatch, capsys):
        """~/.claude absent + claude_code=False: setup must finish (exit 0), not
        crash in append_instructions writing into a non-existent ~/.claude."""
        import shutil
        shutil.rmtree(hermetic_home["claude_dir"], ignore_errors=True)
        monkeypatch.setattr(sh, "detect_environment", lambda *a, **kw: _detected(claude_code=False))
        db = hermetic_home["project"] / "fz.db"
        rc = sh.main(
            ["--yes", "--no-full-stack", "--no-cron", "--no-obsidian-skills",
             "--project-root", str(hermetic_home["project"]), "--db", str(db), "--json"]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["applied"]["hooks"].startswith("skipped")
        assert payload["applied"]["mcp_claude"].startswith("skipped")
        assert not (hermetic_home["claude_dir"] / "CLAUDE.md").exists()  # never created a fake ~/.claude

    def test_db_init_passes_db_before_subcommand(self, hermetic_home, monkeypatch):
        """init-db is invoked as `--db <path> init-db` (global arg first), not
        `init-db --db <path>` which argparse rejects with exit 2."""
        calls: list[list[str]] = []

        def fake_run(args, *a, **kw):
            calls.append(list(args) if isinstance(args, (list, tuple)) else [args])
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sh.subprocess, "run", fake_run)
        monkeypatch.setattr(sh, "detect_environment", lambda *a, **kw: _detected(claude_code=False))
        db = hermetic_home["project"] / "order.db"  # must NOT pre-exist
        sh.main(["--yes", "--no-full-stack", "--no-cron", "--no-obsidian-skills",
                 "--project-root", str(hermetic_home["project"]), "--db", str(db), "--json"])
        init_calls = [c for c in calls if "init-db" in c]
        assert init_calls, "init-db subprocess was never invoked"
        c = init_calls[0]
        assert c.index("--db") < c.index("init-db"), f"--db must precede init-db: {c}"

    def test_verify_only_is_non_interactive_without_yes(self, hermetic_home, monkeypatch):
        """--verify-only (no --yes) must never call input() — stdin may be a
        non-tty (CI/docker/agent), where input() raises EOFError."""
        sh.set_non_interactive(False)
        monkeypatch.setattr("builtins.input", lambda *a, **k: (_ for _ in ()).throw(AssertionError("input() called")))
        monkeypatch.setattr(sh, "detect_environment", lambda *a, **kw: _detected())
        db = hermetic_home["project"] / "vo2.db"
        rc = sh.main(["--verify-only", "--db", str(db), "--json"])
        assert rc == 0
