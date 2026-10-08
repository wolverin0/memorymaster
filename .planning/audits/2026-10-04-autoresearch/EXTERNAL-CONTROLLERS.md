# External autoresearch references (read 2026-10-04)

These notes come from reading through WebFetch, which summarizes the pages. Nothing was installed, cloned or run.

| Repo | SHA read | Role here |
|---|---|---|
| karpathy/autoresearch | `228791fb499a` (2026-03-26) | Model of the loop. The agent edits only `train.py`, while `prepare.py`/`evaluate_bpb` stay read-only. One branch per tag; each change is committed, kept if better, otherwise reset. Runs over budget are discarded. |
| leo-lilinxiao/codex-autoresearch | `0f54c571707` (2026-09-12) | **Candidate controller for Codex.** |
| uditgoenka/autoresearch | `050e30dc4ba0` (2026-08-12) | Model for planning and for regression testing: `--samples 7`, a 5 % noise band, Mann-Whitney U, and every sample in its own process. |
| davebcn87/pi-autoresearch | `939ede8220da` (2026-09-10) | Model for separation: `measure.sh` (metric), `checks.sh` (backpressure) and `log.jsonl` (append-only). |

## codex-autoresearch contract (relevant parts)

- **Verify:** the last non-empty stdout line must be a finite number, or a JSON object read through `--metric-key`. The key must be a top-level numeric value. The command must exit 0 and leave the Git-visible files unchanged. You also pass `--direction lower|higher` and `--target`.
- **Guard:** optional; exit 0 means pass. It must pass at baseline. An improvement that fails the guard is discarded.
- **Commits and reverts:** `commit_trial()` runs `git add` on the scope and then commits. Reverts use `git revert --no-edit`, so history is never rewritten. It requires a clean, named branch. It creates no worktree of its own.
- **Scope:** repeatable repo-relative paths; globs are not allowed.
- **State:** a results directory, either `autoresearch-results/` or `.autoresearch-results/` (the two sources disagree). It holds `run.json`, `events.jsonl`, `runtime.json`, logs and the stop request. There is no lock.
- **Permissions:** background `launch` defaults to `danger-full-access`, which bypasses approvals and the sandbox. Passing `--execution-policy workspace-write` uses the sandbox instead. Whether commits work under the sandbox is unverified.
- **Stopping and budget:**
  - `stop --repo` ends a run, and `status`, `history` and `report` are read-only.
  - The limits are `--max-iterations` and `--timeout-seconds` (default 1800).
  - **There is no cap on money or tokens.**

## Risks and how this repo answers them

- **Evaluator protection only goes as far as `--scope`.** The bench scripts, their baselines and the tests stay **outside** the scope. Each manifest lists only the files that may be edited.
- **No lock, so two controllers on one branch collide.** Run one campaign per dedicated worktree and branch.
- **Full access by default.** Every launch command passes `--execution-policy workspace-write` and runs in its own worktree.
- **No spend cap.** All three proposed campaigns are pure or offline. Verify and Guard make zero provider calls, and the perf gate fails on any outbound connection.
