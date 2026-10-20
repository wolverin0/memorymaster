"""M2 continuity probe: "what did we decide about X" after a simulated compaction (T-1633).

The probe drives the real PreCompact and UserPromptSubmit (recall) hook templates as
subprocesses against a hermetic home, a throwaway database and a fake local LLM
provider. It never touches the authoritative database or ``~/.memorymaster``.

Scripted session: a fixture transcript contains a decision, a rejected option, an
in-flight card id and a planted secret. The compaction summary is simulated as a
summary that DROPS the decision (that is what the probe protects against). The probe
then sends the first prompt after compaction, "what did we decide about the sync retry
backoff", through the recall hook and passes only when the decision reaches the model
context. Recall finds nothing by similarity here (the database is empty), so a pass
can only come from the residual.

Usage::

    python scripts/probe_m2_continuity.py                     # templates in this checkout, residual ON
    python scripts/probe_m2_continuity.py --no-residual       # the pre-change behaviour: expected FAIL
    python scripts/probe_m2_continuity.py --hooks-dir DIR     # any directory of hook templates, e.g. an
                                                              # export of origin/main for the "before" run

Exit status 0 = probe passed, 1 = probe failed.

Real sessions (the due-measurement ``ao1-m2-real-sessions-7d``): for a session that was
really compacted with the residual hook installed, a verifier (never the implementer)
(1) reads the residual claims, ``source_agent = precompact-residual`` with the session id as
citation locator, (2) picks one decision from the turns before the compaction that the
compaction summary does not mention, (3) in the compacted session asks "what did we
decide about <that topic>", and (4) scores PASS when the answer states the decision.
The measurement passes at 4 passing sessions out of 5 compacted real sessions.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "memorymaster" / "config_templates" / "hooks"
PLACEHOLDER = "__MEMORYMASTER_PROJECT_ROOT__"

SESSION_ID = "probe-session-0001"
PROBE_QUESTION = "what did we decide about the sync retry backoff"
DECISION = "use exponential backoff with jitter for sync retries, not a fixed 30 second delay"
DECISION_MARKERS = ("exponential backoff", "jitter")
SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
COMPACTION_SUMMARY = "Summary: the session worked on the sync worker and its tests. No open items recorded."

FAKE_ANSWER = {
    "decisions": [DECISION],
    "rejected": [{"option": "fixed 30 second retry delay", "why": "synchronizes retries into a thundering herd"}],
    "assumptions": ["the upstream API tolerates 5 requests per second"],
    "cards": ["T-4242"],
    "next_step": "add the jitter test to the sync worker suite",
}


def build_transcript(path: Path, cwd: str) -> None:
    """A fixture session: an old compaction boundary, then the turns about to be summarized."""

    def message(role: str, text: str, **extra: object) -> dict:
        return {"type": role, "cwd": cwd, "sessionId": SESSION_ID, "message": {"role": role, "content": text}, **extra}

    lines = [
        message("user", "old topic before the previous compaction that must not be re-summarized"),
        {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted"},
        message("user", COMPACTION_SUMMARY, isCompactSummary=True),
        message("user", "The sync worker keeps hammering the upstream API after an outage. What should we do about retries?"),
        message("assistant", f"Options: a fixed 30 second delay or exponential backoff. We decided to {DECISION}. Card T-4242 tracks it."),
        message("user", f"Agreed. Careful, my token {SECRET} leaked into the log."),
        {"type": "user", "cwd": cwd, "message": {"role": "user", "content": [{"type": "tool_result", "content": "x" * 400}]}},
        message("assistant", "Next I will add the jitter test to the sync worker suite."),
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


class FakeProvider:
    """A local Ollama-shaped HTTP endpoint; records every request body."""

    def __init__(self, answer: object = FAKE_ANSWER, status: int = 200) -> None:
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(json.loads(self.rfile.read(length) or b"{}"))
                body = json.dumps({"response": answer if isinstance(answer, str) else json.dumps(answer)}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def hermetic_env(work: Path, provider_url: str, *, residual: bool, extra: dict[str, str] | None = None) -> dict[str, str]:
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYMASTER_")}
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONPATH": str(REPO_ROOT),
            "MEMORYMASTER_HOOK_STATE_DIR": str(work / "hook_state"),
            "MEMORYMASTER_RECALL_STATE_DIR": str(work / "recall_state"),
            "MEMORYMASTER_CAPTURE_STATE_DB": str(work / "capture.db"),
            "MEMORYMASTER_USAGE_LEDGER_DB": str(work / "usage.db"),
            "MEMORYMASTER_SPOOL_DIR": str(work / "spool"),
            "MEMORYMASTER_DEFAULT_DB": str(work / "probe.db"),
            "MEMORYMASTER_HOOK_RECALL_REMOTE": "0",
            "MEMORYMASTER_LLM_PROVIDER": "ollama",
            "OLLAMA_URL": provider_url,
        }
    )
    if residual:
        env["MEMORYMASTER_PRECOMPACT_RESIDUAL"] = "1"
    env.update(extra or {})
    return env


def stage_hook(template: Path, work: Path) -> Path:
    """Copy a template with the project-root sentinel pointing at an empty throwaway directory."""
    project = work / "project"
    project.mkdir(parents=True, exist_ok=True)
    target = work / "hooks" / template.name
    target.parent.mkdir(parents=True, exist_ok=True)
    text = template.read_text(encoding="utf-8").replace(PLACEHOLDER, str(project).replace("\\", "/"))
    target.write_text(text, encoding="utf-8")
    return target


def run_hook(script: Path, payload: dict, env: dict[str, str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=timeout
    )


def run_probe(hooks_dir: Path, work: Path, *, residual: bool = True) -> dict:
    """Run the scripted session; returns the verdict record."""
    provider = FakeProvider()
    try:
        cwd = str(work / "workspace" / "syncworker")
        Path(cwd).mkdir(parents=True, exist_ok=True)
        transcript = work / "transcript.jsonl"
        build_transcript(transcript, cwd)
        env = hermetic_env(work, provider.url, residual=residual)
        precompact = run_hook(
            stage_hook(hooks_dir / "memorymaster-precompact.py", work),
            {"session_id": SESSION_ID, "transcript_path": str(transcript), "cwd": cwd, "trigger": "auto", "custom_instructions": None},
            env,
        )
        assert DECISION_MARKERS[0] not in COMPACTION_SUMMARY  # the simulated summary dropped the decision
        recall = run_hook(
            stage_hook(hooks_dir / "memorymaster-recall.py", work),
            {"session_id": SESSION_ID, "cwd": cwd, "prompt": PROBE_QUESTION},
            env,
        )
        injected = ""
        if recall.stdout.strip():
            injected = json.loads(recall.stdout)["hookSpecificOutput"]["additionalContext"]
        passed = all(marker in injected.lower() for marker in DECISION_MARKERS)
        return {
            "passed": passed,
            "question": PROBE_QUESTION,
            "provider_calls": len(provider.requests),
            "precompact_exit": precompact.returncode,
            "precompact_stdout_empty": not precompact.stdout.strip(),
            "recall_exit": recall.returncode,
            "injected_chars": len(injected),
            "injected": injected,
            "secret_sent_to_provider": any(SECRET in json.dumps(body) for body in provider.requests),
        }
    finally:
        provider.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--hooks-dir", type=Path, default=TEMPLATES)
    parser.add_argument("--no-residual", action="store_true", help="leave the residual switch off (pre-change behaviour)")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="mm-m2-probe-") as tmp:
        verdict = run_probe(args.hooks_dir, Path(tmp), residual=not args.no_residual)
    print(json.dumps(verdict, indent=2))
    print("PROBE", "PASS" if verdict["passed"] else "FAIL")
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
