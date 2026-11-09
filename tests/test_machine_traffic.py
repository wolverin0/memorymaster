"""T-2273: machine traffic (internal pipelines, pure transport notices) never reaches the recall ranker.

Hooks are rendered from the shipped templates into a tmp dir (fixture ``installed``); the ranker
(``remote.hook_recall``) is replaced by a spy, so nothing touches a database or the network.
"""
from __future__ import annotations

import io
import json
import runpy
import sys

import pytest
from test_codex_recall_hook import installed, invoke  # noqa: F401  (fixture + helper)

from memorymaster.recall import machine_traffic as mt

PURE_NOTICES = {
    "orchestration": "You have 3 orchestration message(s) waiting. Run `orca orchestration check` to read them.",
    "ficha_ping": "[ficha-ping 7 lane=memorymaster]",
    "task_notification": "<task-notification>\n<task-id>b1</task-id>\n<summary>done</summary>\n</task-notification>\n",
    "subagent": '<subagent sessionID="ses_1">',
    "system_notification": "[SYSTEM NOTIFICATION - NOT USER INPUT] disk almost full",
}
STILL_RECALLS = {
    "coordinator_brief": "You are working inside Orca as the memorymaster worker. Please carry out this task from my "
                         "Orca coordinator: explain the recall isolation design.",
    "quoted_notice_midsentence": "why does 'You have 3 orchestration messages' keep appearing in the recall log",
    "notice_then_human_question": "You have 3 orchestration messages waiting, why does the recall hook fire on them?",
    "ping_plus_human_text": "[ficha-ping 3] now explain the recall isolation design",
    "subagent_plus_human_text": "<subagent> tags show up in my logs, explain the recall isolation design",
    "long_orchestration_essay": "You have 3 orchestration messages " + "and the dispatcher keeps retrying them " * 30,
    "plain_human": "explain the recall isolation design",
}
ASK = "explain the recall isolation design"
CONTEXT = "\n- [mm-2bff] a governed cited memory"


@pytest.fixture()
def world(installed, tmp_path, monkeypatch):  # noqa: F811
    from memorymaster.core import hook_log
    from memorymaster.recall import remote

    monkeypatch.setenv("MEMORYMASTER_RECALL_SEEN_DIR", str(tmp_path / "seen"))
    monkeypatch.setattr(hook_log, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(hook_log, "_LOG_FILE", tmp_path / "hook.log")
    monkeypatch.setattr(sys, "path", list(sys.path))  # the templates insert their project root
    monkeypatch.chdir(tmp_path)
    for name in (mt.INTERNAL_ENVS + tuple(mt.FILTER_ENV_PREFIX + a for a in ("CLAUDE", "CODEX", "OPENCODE", "AGY"))):
        monkeypatch.delenv(name, raising=False)
    calls: list[str] = []

    def ranker(query, data, *, db_path):
        calls.append(query)
        return CONTEXT, "local"

    monkeypatch.setattr(remote, "hook_recall", ranker)

    class World:
        ranker_calls = calls
        log = tmp_path / "hook.log"

        def claude(self, prompt: str, session: str = "s-1") -> str:
            out = io.StringIO()
            monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt, "cwd": str(tmp_path),
                                                                      "session_id": session})))
            monkeypatch.setattr(sys, "stdout", out)
            runpy.run_path(str(installed / "memorymaster-recall.py"), run_name="__main__")
            return out.getvalue()

        def codex(self, prompt: str) -> str:
            return invoke(installed, "UserPromptSubmit", {"cwd": str(tmp_path), "prompt": prompt, "session_id": "c-1"},
                          monkeypatch)

        def skip_lines(self) -> list[str]:
            return [ln for ln in self.log.read_text(encoding="utf-8").splitlines() if "machine-traffic" in ln] \
                if self.log.exists() else []

    return World()


def test_zero_of_one_hundred_internal_invocations_reach_the_ranker(world, monkeypatch) -> None:
    prompts = ["Extract at most five candidates that will change future work.", "Compare the 4 candidates with claims.",
               "You are a memory curator. Decide.", ASK]
    for i in range(100):
        on, off = (mt.INTERNAL_ENVS[i % 2], mt.INTERNAL_ENVS[(i + 1) % 2])
        monkeypatch.setenv(on, "1")
        monkeypatch.delenv(off, raising=False)
        assert world.claude(prompts[i % 4], f"internal-{i}") == ""
    assert world.ranker_calls == [], f"{len(world.ranker_calls)}/100 internal invocations reached the ranker"
    assert len(world.skip_lines()) == 100 and all("category=internal_run" in ln for ln in world.skip_lines())


def test_control_without_the_mark_the_ranker_runs(world) -> None:
    assert "a governed cited memory" in world.claude(ASK)
    assert world.ranker_calls == [ASK] and world.skip_lines() == []


def test_pure_transport_notices_skip_the_ranker_and_log_only_the_category(world) -> None:
    for kind, notice in PURE_NOTICES.items():
        assert world.claude(notice, f"n-{kind}") == "", kind
    assert world.ranker_calls == []
    lines = world.skip_lines()
    assert len(lines) == len(PURE_NOTICES)
    assert all("category=transport_notice" in ln and "adapter=claude" in ln for ln in lines)
    assert not any(n.strip()[:24] in ln for ln in lines for n in PURE_NOTICES.values()), "no prompt text in the log"


def test_task_briefs_and_human_prompts_quoting_a_notice_still_recall(world) -> None:
    for kind, prompt in STILL_RECALLS.items():
        assert "a governed cited memory" in world.claude(prompt, f"h-{kind}"), kind
    assert len(world.ranker_calls) == len(STILL_RECALLS) and world.skip_lines() == []


def test_the_codex_adapter_gets_the_filter_from_the_shared_template(world, monkeypatch) -> None:
    assert world.codex(PURE_NOTICES["ficha_ping"]) == ""
    assert world.ranker_calls == []
    assert "adapter=codex" in world.skip_lines()[0]
    assert "a governed cited memory" in world.codex(ASK)


@pytest.mark.parametrize("adapter", ["claude", "codex"])
def test_per_adapter_flag_turns_the_filter_off_for_that_adapter_only(world, monkeypatch, adapter) -> None:
    ask = world.codex if adapter == "codex" else world.claude
    other = world.claude if adapter == "codex" else world.codex
    monkeypatch.setenv(mt.FILTER_ENV_PREFIX + adapter.upper(), "0")
    assert "a governed cited memory" in ask(PURE_NOTICES["orchestration"])  # rolled back: notice recalls again
    assert other(PURE_NOTICES["orchestration"]) == ""  # the other adapter keeps filtering
    assert len(world.ranker_calls) == 1


def test_claude_flag_also_rolls_back_the_internal_run_gate(world, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_INTERNAL_RUN", "1")
    monkeypatch.setenv(mt.FILTER_ENV_PREFIX + "CLAUDE", "off")
    assert "a governed cited memory" in world.claude(ASK)


def test_module_unit_rules() -> None:
    assert mt.is_transport_notice("  \n[ficha-ping 3]  ") and not mt.is_transport_notice(None)
    assert not mt.is_transport_notice("<task-notification>unterminated and then human text")
    assert mt.machine_skip(ASK, "claude", environ={"MEMORYMASTER_INTERNAL_CODEX": "true"}) == "internal_run"
    assert mt.machine_skip(ASK, "claude", environ={}) is None
    assert mt.machine_skip(ASK, "claude", environ={"MEMORYMASTER_INTERNAL_RUN": "0"}) is None


def test_metrics_script_and_hooks_share_one_pattern_list() -> None:
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "scripts"))
    import delivery_metrics

    assert delivery_metrics.TRANSPORT_NOTICE_PATTERNS is mt.TRANSPORT_NOTICE_PATTERNS


def test_opencode_and_agy_adapters_use_the_shared_gate(monkeypatch, tmp_path, capsys) -> None:
    from memorymaster.core import hook_log
    from memorymaster.surfaces import hook_recall

    monkeypatch.setattr(hook_log, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(hook_log, "_LOG_FILE", tmp_path / "hook.log")
    monkeypatch.delenv("MEMORYMASTER_RECALL_MACHINE_FILTER_OPENCODE", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(hook_recall, "hook_recall", lambda q, *a, **k: calls.append(q) or (CONTEXT, "local"))
    monkeypatch.setattr(hook_recall, "format_mode", lambda: "current")

    def run(prompt: str) -> str:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt, "cwd": str(tmp_path)})))
        assert hook_recall.main(["--cli", "opencode"]) == 0
        return capsys.readouterr().out

    assert run(PURE_NOTICES["orchestration"]) == "" and calls == []
    assert "adapter=opencode" in (tmp_path / "hook.log").read_text(encoding="utf-8")
    assert "governed cited memory" in run(ASK)
    monkeypatch.setenv("MEMORYMASTER_RECALL_MACHINE_FILTER_OPENCODE", "0")
    assert "governed cited memory" in run(PURE_NOTICES["orchestration"])


def test_our_headless_claude_calls_carry_the_internal_mark(monkeypatch) -> None:
    import subprocess

    from memorymaster.core import llm_provider

    seen: dict = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(llm_provider, "_resolve_claude_bin", lambda: "claude")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert llm_provider._call_claude_cli("p", "t") == "ok"
    assert seen["env"]["MEMORYMASTER_INTERNAL_RUN"] == "1"
