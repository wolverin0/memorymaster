"""Intent-anchored tests for the PreToolUse grep/glob recall-inject hook.

The feature (source key: pretooluse_grep_inject, borrowed from
codebase-memory-mcp) intercepts Grep/Glob tool calls and injects relevant
MemoryMaster memory as `additionalContext`. It MUST be default-OFF, because
it stacks a second recall injection on top of the existing UserPromptSubmit
recall hook — turning it on by default would double every agent's recall
cost without consent.

These tests run the ACTUAL shipped template file as a subprocess (exactly
how Claude Code invokes a hook), with a stub `memorymaster` package on
PYTHONPATH so `recall()` is mocked and no real DB is touched. Asserting on
the real process stdout — not a mirror of the logic — is what makes these
regression-proof: if the hook stops honoring the flag, or stops calling
recall, or crashes on bad input, a test goes red.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TEMPLATE = (
    Path(__file__).parent.parent
    / "memorymaster"
    / "config_templates"
    / "hooks"
    / "memorymaster-pretooluse-recall.py"
)

# Sentinel the stub recall() emits so we can prove the hook called it and
# forwarded its output verbatim into additionalContext.
RECALL_SENTINEL = "STUB-RECALL-MEMORY-BLOCK-42"


def _prepare_hook(tmp_path: Path) -> Path:
    """Materialize the template with its placeholder pointed at a stub package.

    The stub `memorymaster` package provides:
      - core.hook_log.log_hook  (no-op, so the hook's logging never touches disk)
      - recall.context_hook.recall  (records the call + returns the sentinel)
    A marker file proves whether recall() was actually invoked.
    """
    root = tmp_path / "proj"
    pkg = root / "memorymaster"
    (pkg / "core").mkdir(parents=True)
    (pkg / "recall").mkdir(parents=True)

    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "recall" / "__init__.py").write_text("", encoding="utf-8")

    (pkg / "core" / "hook_log.py").write_text(
        "def log_hook(hook, event, **fields):\n    pass\n",
        encoding="utf-8",
    )
    marker = root / "recall_called.txt"
    # repr() the path so Windows backslashes don't become invalid escapes in
    # the generated source (e.g. C:\Users -> \U... truncated-escape SyntaxError).
    (pkg / "recall" / "context_hook.py").write_text(
        "def recall(query, *, db_path='', skip_qdrant=False, **kwargs):\n"
        f"    open({str(marker)!r}, 'w', encoding='utf-8').write(query)\n"
        f"    return {RECALL_SENTINEL!r}\n",
        encoding="utf-8",
    )

    src = TEMPLATE.read_text(encoding="utf-8")
    src = src.replace("__MEMORYMASTER_PROJECT_ROOT__", str(root))
    hook = tmp_path / "hook.py"
    hook.write_text(src, encoding="utf-8")
    return hook


def _run(hook: Path, payload: str | None, env_flag: str | None) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    env.pop("MEMORYMASTER_PRETOOLUSE_RECALL", None)
    if env_flag is not None:
        env["MEMORYMASTER_PRETOOLUSE_RECALL"] = env_flag
    return subprocess.run(
        [sys.executable, str(hook)],
        input=payload if payload is not None else "",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _grep_payload() -> str:
    return json.dumps({
        "tool_name": "Grep",
        "session_id": "sess-abc",
        "tool_input": {"pattern": "auth token validation"},
    })


def test_flag_unset_injects_nothing(tmp_path: Path) -> None:
    """Default-OFF invariant: with the flag unset the hook is a pure passthrough.

    This anchors on WHY the feature is opt-in — it must never add a second
    recall injection unless the operator explicitly asked for it. Even a
    perfectly valid Grep payload produces no additionalContext.
    """
    hook = _prepare_hook(tmp_path)
    marker = tmp_path / "proj" / "recall_called.txt"

    result = _run(hook, _grep_payload(), env_flag=None)

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert "additionalContext" not in result.stdout
    assert not marker.exists(), "recall() must NOT be called when the flag is unset"


def test_flag_set_calls_recall_and_injects_context(tmp_path: Path) -> None:
    """Opt-in path: flag=1 + a Grep call => recall() runs and its block is injected.

    Anchors on the feature's purpose: the query the agent is about to grep
    for is fed to recall, and recall's memory block is surfaced back as
    PreToolUse additionalContext.
    """
    hook = _prepare_hook(tmp_path)
    marker = tmp_path / "proj" / "recall_called.txt"

    result = _run(hook, _grep_payload(), env_flag="1")

    assert result.returncode == 0, result.stderr
    assert marker.exists(), "recall() must be called when the flag is set"
    assert marker.read_text(encoding="utf-8") == "auth token validation"

    out = json.loads(result.stdout)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert RECALL_SENTINEL in hso["additionalContext"]


def test_non_search_tool_is_ignored_even_when_enabled(tmp_path: Path) -> None:
    """Enabled, but the tool isn't Grep/Glob => no recall, no injection.

    The feature is scoped to search tools; injecting on unrelated tools
    would be scope creep and waste recall cost.
    """
    hook = _prepare_hook(tmp_path)
    marker = tmp_path / "proj" / "recall_called.txt"
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la some directory here"},
    })

    result = _run(hook, payload, env_flag="1")

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert not marker.exists()


def test_malformed_stdin_exits_zero_without_crashing(tmp_path: Path) -> None:
    """Robustness: garbage on stdin must never break the intercepted tool call.

    A hook that crashes (nonzero exit / traceback on stdout) could block the
    user's Grep entirely, so bad input must fail closed and silent.
    """
    hook = _prepare_hook(tmp_path)

    result = _run(hook, "this is not json {{{", env_flag="1")

    assert result.returncode == 0
    assert "additionalContext" not in result.stdout


def test_empty_stdin_exits_zero(tmp_path: Path) -> None:
    """Empty stdin (no payload) is a no-op passthrough, not an error."""
    hook = _prepare_hook(tmp_path)

    result = _run(hook, "", env_flag="1")

    assert result.returncode == 0
    assert result.stdout.strip() == ""
