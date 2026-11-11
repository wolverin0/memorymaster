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
    "orchestration": "You have 3 orchestration message(s). Run `orca orchestration check --run run_ab12cd`.",
    "orchestration_plural": "You have 12 orchestration messages. Run `orca orchestration check --run run_ab12cd`.",
    "ficha_ping": "[ficha-ping:memorymaster:7]",
    "task_notification": "<task-notification>\n<task-id>b1</task-id>\n<summary>done</summary>\n</task-notification>\n",
    "subagent": '<subagent sessionID="ses_1">',
    "subagent_element": '<subagent sessionID="ses_1">finished</subagent>',
    "system_notification": "[SYSTEM NOTIFICATION - NOT USER INPUT]\nThis is an automated background-task event.\n\n"
                           "<task-notification>\n<task-id>b1</task-id>\n</task-notification>",
}
STILL_RECALLS = {
    "coordinator_brief": "You are working inside Orca as the memorymaster worker. Please carry out this task from my "
                         "Orca coordinator: explain the recall isolation design.",
    "quoted_notice_midsentence": "why does 'You have 3 orchestration messages' keep appearing in the recall log",
    "notice_then_human_question": "You have 3 orchestration messages waiting, why does the recall hook fire on them?",
    "long_orchestration_essay": "You have 3 orchestration messages " + "and the dispatcher keeps retrying them " * 30,
    "ping_with_body": "[ficha-ping:memorymaster:3] now explain the recall isolation design",
    "plain_human": "explain the recall isolation design",
    # review of PR 374: a notice plus a request is a human request
    "orchestration_plus_fix_request": "You have 3 orchestration messages waiting. Fix the recall hook so it ignores them.",
    "orchestration_plus_spanish": "You have 3 orchestration message(s). Run `orca orchestration check --run run_ab12cd`. "
                                  "Explicame por que el hook dispara en ellos",
    "system_notification_plus_request": "[SYSTEM NOTIFICATION - NOT USER INPUT] disk full. please clean up logs and report",
    "ping_plus_array_fix": "[ficha-ping 3] please fix the bug in arr[0]",
    "subagent_tag_regex_fix": "<subagent> tag is broken in the parser, fix the regex a->b>",
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


# is_automated (recall/delivery.py) predates this card and skips the literal "[SYSTEM NOTIFICATION - NOT USER INPUT]"
# header by prefix, which test_recall_delivery pins; that header says it is not a request, so the end-to-end hook
# still skips that one. is_transport_notice itself (asserted in test_module_unit_rules) does not claim it.
PREFIX_AUTOMATED = {"system_notification_plus_request"}


def test_task_briefs_and_human_prompts_quoting_a_notice_still_recall(world) -> None:
    kinds = [k for k in STILL_RECALLS if k not in PREFIX_AUTOMATED]
    for kind in kinds:
        assert "a governed cited memory" in world.claude(STILL_RECALLS[kind], f"h-{kind}"), kind
    assert len(world.ranker_calls) == len(kinds) and world.skip_lines() == []


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


@pytest.mark.parametrize("adapter", ["claude", "codex"])
def test_flag_also_rolls_back_the_internal_run_gate(world, monkeypatch, adapter) -> None:
    ask = world.codex if adapter == "codex" else world.claude
    monkeypatch.setenv("MEMORYMASTER_INTERNAL_RUN", "1")
    assert ask(ASK) == "" and world.ranker_calls == []  # filter on: skipped (Codex: before stdin is read)
    monkeypatch.setenv(mt.FILTER_ENV_PREFIX + adapter.upper(), "off")
    assert "a governed cited memory" in ask(ASK)


def test_codex_adapter_falls_back_to_the_inline_check_on_an_older_package(world, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "memorymaster.recall.machine_traffic", None)  # import raises ImportError
    monkeypatch.setenv("MEMORYMASTER_INTERNAL_CODEX", "1")
    assert world.codex(ASK) == "" and world.ranker_calls == []
    monkeypatch.delenv("MEMORYMASTER_INTERNAL_CODEX")
    assert world.codex(ASK) != ""  # an older package must not silence Codex recall


def test_module_unit_rules() -> None:
    assert mt.is_transport_notice("  \n[ficha-ping 3]  ") and not mt.is_transport_notice(None)
    assert not mt.is_transport_notice("<task-notification>unterminated and then human text")
    for kind, prompt in STILL_RECALLS.items():
        assert not mt.is_transport_notice(prompt), kind
    for kind, notice in PURE_NOTICES.items():
        assert mt.is_transport_notice("\n  " + notice + "  \n"), kind
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


# --- audit D-6: mixed-prompt rule. (label, prompt, internal env, should_skip) ---------------------------------
_N = "You have 2 orchestration messages. Run `orca orchestration check --run run_ab12cd34`."
_Z = "zorblax cache invalidation strategy"
_TASK = f"<task-notification><task-id>b5k27</task-id><status>completed</status><summary>{_Z} job done</summary></task-notification>"
MACHINE_TABLE = [
    ("orca notice pure", _N, False, True),
    ("orca notice + trailing newline", _N + "\n", False, True),
    ("orca notice (s) form", "You have 1 orchestration message(s). Run `orca orchestration check --run run_ff00ee11`.", False, True),
    ("two orca notices stacked", _N + "\n" + _N, False, True),
    ("orca notice wrapped by terminal header", "──── From: TERM_1A2B-FAN ────\n" + _N, False, True),
    ("ficha-ping bare", "[ficha-ping 2026-11-10T12:00 lane=memorymaster]", False, True),
    ("task-notification", _TASK, False, True),
    ("system notification envelope", f"[SYSTEM NOTIFICATION - not user input]\n<task-notification><summary>{_Z}</summary></task-notification>", False, True),
    ("agent-message from subagent", f'<agent-message from="a1b2c3">{_Z} check finished, see report</agent-message>', False, True),
    ("extractor prompt, env mark set", f"Extract at most five durable facts about the {_Z} from this transcript.", True, True),
    ("extractor prompt, NO env mark", f"Extract at most five stable facts, decisions, preferences, profiles, or constraints about the {_Z}.", False, True),
    ("judge prompt, NO env mark", f"You are a relevance judge. Given a USER QUERY and a list of CANDIDATE memory claims, return the subset about the {_Z}.", False, True),
    ("curator prompt, NO env mark", f"You are a memory curator for an AI coding agent system. Given a raw memory text about the {_Z}, extract claims.", False, True),
    ("human prompt", f"which strategy did we choose for the {_Z}", False, False),
    ("human quoting orca notice", f"I got this: '{_N}' - which strategy did we choose for the {_Z}?", False, False),
    ("notice followed by human ask", _N + f"\nBefore that: which strategy did we choose for the {_Z}?", False, False),
    ("human quoting task-notification", f"what does this mean <task-notification><summary>x</summary></task-notification> for the {_Z}?", False, False),
    ("human quoting extractor prompt", f"why does the prompt 'Extract at most five durable facts' miss the {_Z}?", False, False),
    ("coordinator task brief", f"You are working inside Orca. Task: review the {_Z} and report.", False, False),
    # new machine shapes
    ("notice with trailing space", _N + " ", False, True),
    ("three notices and a ping", _N + "\n" + _N + "\n[ficha-ping 3]\n" + _N, False, True),
    ("teammate message element", '<teammate-message teammate_id="a" summary="s">status: tests green</teammate-message>', False, True),
    ("cross-session message element", '<cross-session-message from="run:abc">fyi build done</cross-session-message>', False, True),
    ("task notification then orca notice", _TASK + "\n" + _N, False, True),
    ("two teammate messages", "<teammate-message>a b</teammate-message>\n<teammate-message>c d</teammate-message>", False, True),
    ("bare subagent tag with punctuation", '<subagent sessionID="s1"> .', False, True),
    ("quality evaluator prompt with leading blank lines", "\n\nYou are a memory quality evaluator. Two claims contradict each other. Pick one.", False, True),
    ("consolidation prompt", "Compare the 4 candidates with governed current claims. Return one verdict each.", False, True),
    # new human negatives that look similar
    ("human pastes notice and asks meaning", _N + "\nwhat does this mean?", False, False),
    ("human asks about notice before pasting", f"what does this mean? {_N}", False, False),
    ("human asks about task-notification tag", "why is <task-notification> never closed in my transcript", False, False),
    ("human quotes closed task-notification in a question", f"{_TASK} why did it say completed?", False, False),
    ("mentions ping", "why does the ping from the dispatcher arrive twice", False, False),
    ("mentions ficha-ping in a sentence", "how do I stop [ficha-ping 3] from showing up in the status bar", False, False),
    ("long human prompt then short notice", "Please review the retention migration plan in docs and list the risks, in order of severity. " * 4 + _N, False, False),
    ("notice then long human prompt", _N + "\n" + "Also explain how the steward decides promotion. " * 5, False, False),
    ("ping plus request", "[ficha-ping 3] please fix the bug in arr[0]", False, False),
    ("teammate message plus ask", "<teammate-message>tests green</teammate-message> now update the changelog", False, False),
    ("human quoting teammate tag", "the <teammate-message> wrapper breaks my parser, how do I strip it", False, False),
    ("cross-session notice plus question", '<cross-session-message from="x">done</cross-session-message> which branch is it on?', False, False),
    ("human mentions orchestration messages", "You have 3 orchestration messages waiting, why does the hook fire on them?", False, False),
    ("human quotes curator mid-sentence", "the prompt You are a memory curator is stored as a claim, is that a bug?", False, False),
    ("human asks about the system notification header", "[SYSTEM NOTIFICATION - NOT USER INPUT] where does this header come from?", False, False),
    ("human says continue", "continue", False, False),
    ("spanish question after notice", _N + " Explicame por que llego dos veces", False, False),
]


@pytest.mark.parametrize("label,prompt,internal,should_skip", MACHINE_TABLE, ids=[row[0] for row in MACHINE_TABLE])
def test_machine_table(label, prompt, internal, should_skip) -> None:
    environ = {"MEMORYMASTER_INTERNAL_RUN": "1"} if internal else {}
    assert (mt.machine_skip(prompt, "claude", environ=environ) is not None) is should_skip, label


def test_machine_table_totals() -> None:
    original = MACHINE_TABLE[:19]
    machine = [r for r in original if r[3]]
    human = [r for r in original if not r[3]]
    assert (len(machine), len(human)) == (13, 6)
    assert sum(mt.machine_skip(r[1], "claude", environ={"MEMORYMASTER_INTERNAL_RUN": "1"} if r[2] else {}) is not None
               for r in machine) == 13
    assert sum(mt.machine_skip(r[1], "claude", environ={}) is None for r in human) == 6
    assert sum(1 for r in MACHINE_TABLE[19:] if not r[3]) >= 12


def test_the_per_adapter_rollback_still_covers_the_new_shapes() -> None:
    env = {mt.FILTER_ENV_PREFIX + "AGY": "0"}
    assert mt.machine_skip(_N + "\n" + _N, "agy", environ=env) is None
    assert mt.machine_skip(_N + "\n" + _N, "claude", environ=env) == "transport_notice"


# --- verifier round: bounded scan, agent-message status rule, pinned internal heads, boilerplate residue ---------
import time  # noqa: E402
from pathlib import Path  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("unit", [
    "<subagent sessionID=\"s\">", "<agent-message>", "<teammate-message from=\"a\">", "<cross-session-message>",
    "<task-notification>", "[SYSTEM NOTIFICATION - x] ", "[ficha-ping ", "You have 1 orchestration message. Run `",
    "---- From: x ", "─" * 8, "<agent-message<agent-message", "</agent-message>",
])
def test_unclosed_or_repeated_shapes_stay_linear_on_a_300kb_prompt(unit) -> None:
    prompt = unit * (300_000 // len(unit))
    start = time.perf_counter()
    mt.machine_skip(prompt, "claude", environ={})
    assert time.perf_counter() - start < 0.5, unit


def test_many_closed_elements_with_one_distant_close_stay_linear() -> None:
    prompt = "<agent-message>" * 15000 + "</agent-message>"
    start = time.perf_counter()
    mt.machine_skip(prompt, "claude", environ={})
    assert time.perf_counter() - start < 0.5


_BRIEF = "Please carry out this task and report back with the evidence. " * 6  # about 360 chars of prose
_PAD = "x" * 250
STATUS_TABLE = [
    ("short teammate text", "<teammate-message>tests green, nothing to do</teammate-message>", True),
    ("json idle notification, long", '<teammate-message>{"type":"idle_notification","from":"w","note":"' + _PAD + '"}</teammate-message>', True),
    ("json shutdown request", '<teammate-message>{"type":"shutdown_request","reason":"' + _PAD + '"}</teammate-message>', True),
    ("json plan approval response", '<teammate-message>{"type":"plan_approval_response","approve":true,"n":"' + _PAD + '"}</teammate-message>', True),
    ("json other type, long", '<teammate-message>{"type":"task_assignment","text":"' + _PAD + '"}</teammate-message>', False),
    ("long prose brief, teammate", "<teammate-message teammate_id=\"lead\">" + _BRIEF + "</teammate-message>", False),
    ("long prose brief, agent-message", "<agent-message from=\"a\">" + _BRIEF + "</agent-message>", False),
    ("long prose brief, cross-session", "<cross-session-message from=\"run:1\">" + _BRIEF + "</cross-session-message>", False),
    ("long prose brief, subagent", "<subagent sessionID=\"s\">" + _BRIEF + "</subagent>", False),
    ("short status, cross-session", "<cross-session-message from=\"run:1\">build done</cross-session-message>", True),
    ("task-notification is always a notice", "<task-notification><summary>" + _BRIEF + "</summary></task-notification>", True),
    ("brief after a status notice", "<teammate-message>ok</teammate-message>\n<teammate-message>" + _BRIEF + "</teammate-message>", False),
    ("unclosed long agent tag is plain text", "<agent-message from=\"a\">" + _BRIEF, False),
]


@pytest.mark.parametrize("label,prompt,should_skip", STATUS_TABLE, ids=[row[0] for row in STATUS_TABLE])
def test_agent_message_is_a_notice_only_when_it_is_status(label, prompt, should_skip) -> None:
    assert (mt.machine_skip(prompt, "claude", environ={}) is not None) is should_skip, label


HEAD_SOURCES = {
    "Extract at most five candidates that will change future work.": "memorymaster/dreaming/providers.py",
    "Extract at most five stable facts, decisions, preferences, profiles, or": "memorymaster/dreaming/providers.py",
    "Extract at most 3 non-obvious bug causes, decisions, gotchas, or constraints.": "memorymaster/surfaces/session_end_ingest.py",
    "You are a memory curator. Extract max 3 non-obvious learnings.": "memorymaster/config_templates/hooks/memorymaster-auto-ingest.py",
    "You are a memory curator for an AI coding agent system.": "memorymaster/govern/llm_steward.py",
    "You are a memory quality evaluator. Two claims contradict each other.": "memorymaster/govern/auto_resolver.py",
    "You are preserving working context that a conversation compaction is about to destroy.": "memorymaster/capture/precompact_residual.py",
    "You are a bounded reviewer of recurring agent workflow evidence.": "memorymaster/knowledge/skills.py",
    "You are a knowledge curator. Given a batch of claims from a coding project": "memorymaster/knowledge/vault_curator.py",
    "You are a knowledge base auditor. For each potential contradiction": "memorymaster/knowledge/vault_linter.py",
    "You are a technical writer creating a wiki article about a software project.": "memorymaster/knowledge/wiki_engine.py",
    "You are a wiki editor. Review this article and rate it 1-10:": "memorymaster/knowledge/wiki_engine.py",
    "You judge whether stored memory claims are relevant to a user's message": "scripts/eval_index_vs_similarity.py",
    "You are auditing the long-term memory store of a coding-agent system": "scripts/judge_claim_value.py",
    "You are a relevance judge. Given a USER QUERY and a list of CANDIDATE memory claims": "scripts/label_prompts_with_judge.py",
}


def test_internal_heads_are_the_real_prompt_openings() -> None:
    assert set(HEAD_SOURCES) == set(mt.INTERNAL_PROMPT_HEADS)
    for head, source in HEAD_SOURCES.items():
        assert head in (REPO / source).read_text(encoding="utf-8"), head
        assert len(head) <= 96, head  # the metrics script only sees the first 96 chars of a stored query
    assert "candidates with governed current claims." in (REPO / "memorymaster/dreaming/providers.py").read_text(encoding="utf-8")


def test_internal_heads_skip_and_the_metrics_script_reads_the_same_list() -> None:
    sys.path.insert(0, str(REPO / "scripts"))
    import delivery_metrics

    heads = list(HEAD_SOURCES) + ["Compare the 4 candidates with governed current claims. Return one"]
    for head in heads:
        assert mt.machine_skip(head + " rest of the prompt", "claude", environ={}) == "internal_prompt", head
        assert delivery_metrics.classify_query(head[:96]) == "internal_process", head
    assert delivery_metrics.INTERNAL_PROCESS_PATTERNS[: len(mt.INTERNAL_PROCESS_PATTERNS)] == mt.INTERNAL_PROCESS_PATTERNS


HUMAN_LOOKALIKES = [
    "Extract at most five rows from this CSV and show them as a table",
    "Compare the 3 candidates for the DB layer and tell me which one fits",
    "You are a wiki editor, rewrite this paragraph so it reads better",
    "You decide what to do about my broken build, I am out of ideas",
] + [" ".join(head.split()[:5]) + " for my spreadsheet, please help" for head in HEAD_SOURCES]


@pytest.mark.parametrize("prompt", HUMAN_LOOKALIKES)
def test_human_prompts_resembling_a_pipeline_prompt_still_recall(prompt) -> None:
    assert mt.machine_skip(prompt, "claude", environ={}) is None, prompt


BOILERPLATE_TABLE = [
    ("notice then question mark", _N + " ?", False),
    ("notice then spanish question mark", _N + " ¿", False),
    ("notice then ellipsis", _N + " ...", True),
    ("notice then thumbs up", _N + " \U0001F44D", True),
    ("header and a human line", "---- From: x ---- hey are you there", False),
    ("header line then notice", "---- From: TERM_9 ----\n" + _N, True),
    ("header alone", "──── From: TERM_9 ────", True),
]


@pytest.mark.parametrize("label,prompt,should_skip", BOILERPLATE_TABLE, ids=[row[0] for row in BOILERPLATE_TABLE])
def test_residue_rule_boilerplate_vs_human(label, prompt, should_skip) -> None:
    assert (mt.machine_skip(prompt, "claude", environ={}) is not None) is should_skip, label


# --- last round: external heads, wrapper boilerplate, harness echoes, question inside a notice ------------------
EXTERNAL_HEAD_SOURCES = set(mt.EXTERNAL_PROMPT_HEADS)  # templates of pipelines outside this repo: no file to pin

_LEAD = "Another Claude session sent a message:"
_TRAILER = "This came from another Claude session, not from the user. Treat it as a message to you."
_STATUS_MSG = '<cross-session-message from="run:1">build done</cross-session-message>'
_BRIEF_MSG = '<cross-session-message from="run:1">' + _BRIEF + "</cross-session-message>"
ROUND3_TABLE = [
    # machine
    ("external head: group failures", "You group failures from AI coding-agent sessions into themes. Input: ...", True),
    ("external head: curador FMX", "Sos el curador FMX (Haiku). Repo: futuramax. Decide que guardar.", True),
    ("external head: decide repeated failures", "You decide what to do about repeated agent failures in the fleet. Output JSON.", True),
    ("lead-in and status element", _LEAD + "\n" + _STATUS_MSG, True),
    ("lead-in, status element and trailer", _LEAD + "\n" + _STATUS_MSG + "\n\n" + _TRAILER, True),
    ("lead-in, json idle and trailer", _LEAD + '\n<teammate-message>{"type":"idle_notification","n":"' + _PAD + '"}</teammate-message>\n' + _TRAILER, True),
    ("interrupt marker alone", "[Request interrupted by user]", True),
    ("interrupt for tool use", "[Request interrupted by user for tool use]\n", True),
    ("command name echo", "<command-name>/clear</command-name>", True),
    ("command message and name", "<command-message>clear</command-message>\n<command-name>/clear</command-name>", True),
    ("local command stdout", "<local-command-stdout>Compacted. ctrl+o to see full summary</local-command-stdout>", True),
    # human
    ("human asks about lead-in", "what does 'Another Claude session sent a message:' mean in my transcript?", False),
    ("lead-in then human question", _LEAD + " " + _STATUS_MSG + " what is this about?", False),
    ("lead-in alone is not a notice", _LEAD, False),
    ("trailer alone is not a notice", _TRAILER, False),
    ("lead-in and brief still recalls", _LEAD + "\n" + _BRIEF_MSG + "\n\n" + _TRAILER, False),
    ("human asks about interrupt marker", "why do I see [Request interrupted by user] after every esc?", False),
    ("interrupt marker then request", "[Request interrupted by user] now run the tests again", False),
    ("command name then args", "<command-name>/fix</command-name>\n<command-args>the login redirect loop</command-args>", False),
    ("human quotes command tag", "what does the <command-name> tag in my history do", False),
    ("stdout then question", "<local-command-stdout>ok</local-command-stdout> why did it print that", False),
    ("question inside task-notification", "<task-notification> what is this? </task-notification>", False),
    ("question inside task-notification with tags", "<task-notification><summary>did the job finish?</summary></task-notification>", False),
]


@pytest.mark.parametrize("label,prompt,should_skip", ROUND3_TABLE, ids=[row[0] for row in ROUND3_TABLE])
def test_round3_shapes(label, prompt, should_skip) -> None:
    assert (mt.machine_skip(prompt, "claude", environ={}) is not None) is should_skip, label


def test_external_heads_are_full_phrases_shared_with_the_metrics_script() -> None:
    sys.path.insert(0, str(REPO / "scripts"))
    import delivery_metrics

    assert EXTERNAL_HEAD_SOURCES.isdisjoint(HEAD_SOURCES)
    assert all(len(head) <= 96 for head in EXTERNAL_HEAD_SOURCES)
    for head in EXTERNAL_HEAD_SOURCES:
        assert delivery_metrics.classify_query(head) == "internal_process", head
    assert not any(p.pattern.startswith(("You group", "Sos el", "You decide")) for p in delivery_metrics.INTERNAL_PROCESS_PATTERNS
                   if p not in mt.INTERNAL_PROCESS_PATTERNS)  # no local copy: it all comes from the shared list
    assert mt.machine_skip("You decide what to do about my broken build", "claude", environ={}) is None


@pytest.mark.parametrize("unit", ["[Request interrupted by user", "<command-name>", "<local-command-stdout>",
                                  "<command-message>", "Another Claude session sent a message:", "This came from another Claude session"])
def test_new_shapes_stay_linear_on_a_300kb_prompt(unit) -> None:
    prompt = unit * (300_000 // len(unit))
    start = time.perf_counter()
    mt.machine_skip(prompt, "claude", environ={})
    assert time.perf_counter() - start < 0.5, unit
