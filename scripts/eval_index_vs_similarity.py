"""T-1433 AC3: index + on-demand reading versus similarity injection, on real prompts.

Measures, for the automatic prompt-recall hook, two delivery strategies over the SAME
authorized dense candidate pool:

* A, similarity injection (production): the first ``hook_policy.max_claims()`` bodies of
  the ranked pool, rendered exactly as the hook renders them.
* B, index + on-demand reading: a compact index (id + 72-character title) of the first
  ``--index-k`` pool claims; a reader model sees only the prompt and the index and picks
  the bodies it would read. Its cost is the index plus every body it asked for.

Relevance comes from two judges of different model families (Gemini 4 Argon through the
Antigravity CLI and Claude through ``claude -p``). Each judge sees the prompt and every pool
body, shuffled and labelled C1..Cn: it never learns which strategy surfaced a claim. Both
strategies are scored against the same labels.

Safety contract:
  * The claims DB is only the snapshot passed with ``--snapshot`` (make it with the sqlite3
    backup API from a ``mode=ro`` connection); it is opened read-only. Nothing calls
    ``_finish_recall``, so no access record is spooled, no session state is written and no
    telemetry line is appended. The dense service is queried (read-only) for candidate ids.
  * Prompts come from the Claude Code transcripts (read-only). Prompts the sensitivity scan
    flags are dropped; home paths and private IPv4 addresses are masked in every prompt and
    claim body sent to a model. Prompt and claim text stay under ``--out-dir``.
  * The public report (``report``) prints only ordinals, counts, labels and a checksum.

Usage (each step resumes from the files already in OUT):
    python scripts/eval_index_vs_similarity.py collect  --out-dir OUT --since 2026-10-23 --until 2026-10-30
    python scripts/eval_index_vs_similarity.py pool     --out-dir OUT --snapshot SNAP.db
    python scripts/eval_index_vs_similarity.py read     --out-dir OUT
    python scripts/eval_index_vs_similarity.py judge    --out-dir OUT
    python scripts/eval_index_vs_similarity.py report   --out-dir OUT
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.judge_claim_value import mask_outgoing  # noqa: E402
from scripts.review_recall_citations import is_real_prompt, parse_time, prompt_text  # noqa: E402

SEED_PREFIX = "T-1433-ac3"
PROMPT_KEEP = 4000
TITLE_CHARS = 72
INDEX_HEADER = "[Memory index; read a claim body by id only when needed]"
POOL_K = 12  # dense_recall.FETCH_K: everything the service returns above threshold
MAX_READS = 3
LABELS = ("R", "I", "U")
GEMINI_JUDGE = "gemini-4-argon-medium"
CLAUDE_JUDGE = "sonnet"
CLAUDE_READER = "haiku"

RUBRIC = """You judge whether stored memory claims are relevant to a user's message to a coding agent.
For EACH candidate claim give one label:
- R: directly usable for what the message asks: a constraint, fact, decision or caveat the agent
  would act on while doing this request.
- I: shares only a project, word or broad theme; concerns another component or action; or is an
  old task/incident with no demonstrated bearing on this request. Generic lessons are not R.
- U: the message cannot be understood without missing conversation or an image.
Answer with a JSON array only, one object per candidate, no prose and no code fence:
[{"c": "C1", "label": "R|I|U"}]"""

READER = """You are a coding agent about to act on the user's message below. An automatic memory
index lists stored claims by id and a short title. Reading a claim's full body costs context, so
read only the claims whose body would likely change or inform what you do for THIS message.
Choose between 0 and {max_reads} ids. Answer with JSON only, no prose and no code fence:
{{"read": ["mm-..."]}}"""


# ----------------------------------------------------------------------------- io


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------ collect


def eligible_prompt(text: str, cwd: str) -> str | None:
    """Return why the hook (or the privacy rule) would skip this prompt, or None when it is eligible."""
    from memorymaster.core.security import scan_text_for_findings
    from memorymaster.recall import hook_policy
    from memorymaster.recall.delivery import is_automated

    if not text.strip():
        return "empty"
    if is_automated(text):
        return "automated"
    if hook_policy.is_trivial_prompt(text):
        return "trivial"
    if hook_policy.disabled_for(cwd):
        return "hook_disabled_cwd"
    if scan_text_for_findings(text):
        return "sensitive"
    return None


def collect(projects: Path, since: datetime, until: datetime, n: int, seed: str) -> tuple[list[dict], dict]:
    """Real top-level prompts in [since, until), deduplicated by text, sampled by a seeded hash order."""
    reasons: dict[str, int] = {}
    seen_text: set[str] = set()
    prompts: list[dict] = []
    for path in sorted(projects.glob("*/*.jsonl")):
        if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < since:
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict) or row.get("isSidechain") or not is_real_prompt(row):
                    continue
                ts = parse_time(row.get("timestamp"))
                if ts is None or not since <= ts < until:
                    continue
                text = prompt_text(row)
                cwd = str(row.get("cwd") or "")
                reason = eligible_prompt(text, cwd)
                if reason is None and text in seen_text:
                    reason = "duplicate_text"
                if reason:
                    reasons[reason] = reasons.get(reason, 0) + 1
                    continue
                seen_text.add(text)
                uuid = str(row.get("uuid") or _sha(text))
                prompts.append({"uuid": uuid, "ts": ts.isoformat(), "cwd": cwd, "prompt": text})
    prompts.sort(key=lambda p: _sha(f"{SEED_PREFIX}:{seed}:{p['uuid']}"))
    stats = {"eligible": len(prompts), "excluded": reasons, "sampled": min(n, len(prompts))}
    return prompts[:n], stats


# --------------------------------------------------------------------------- pool


def title_of(body: str) -> str:
    return re.sub(r"\s+", " ", body).strip()[:TITLE_CHARS]


def index_text(candidates: list[dict]) -> str:
    """The compact index B injects; empty when there is nothing to index."""
    if not candidates:
        return ""
    return INDEX_HEADER + "\n" + "\n".join(f"- [{c['cid']}] {title_of(c['body'])}" for c in candidates)


def read_text(candidates: list[dict], read_ids: list[str]) -> str:
    """The bodies returned to B's on-demand reads, in the order requested."""
    by_id = {c["cid"]: c for c in candidates}
    return "\n\n".join(f"[{cid}]\n{by_id[cid]['body']}" for cid in read_ids if cid in by_id)


def pool_for(prompt: dict, svc) -> dict:
    """The authorized dense pool and A's rendered block, with every hook side effect left out."""
    from memorymaster.core.scope_utils import scope_from_cwd
    from memorymaster.recall import context_hook, hook_lexical, hook_policy

    cwd = prompt["cwd"]
    scope_token = context_hook._REQUEST_SCOPE.set(scope_from_cwd(cwd) if cwd else None)
    parents = list(Path(cwd).parents)[:2] if cwd else []
    shared = tuple(dict.fromkeys(s for s in (scope_from_cwd(str(p)) for p in parents) if s.startswith("project:")))
    shared_token = context_hook._REQUEST_SHARED_SCOPES.set(shared)
    try:
        plan = context_hook._prompt_recall_plan(prompt["prompt"], limit=100)
        rows, metrics = context_hook._dense_prompt_rows(svc, prompt["prompt"], plan)
        if rows is None:
            return {"uuid": prompt["uuid"], "status": "dense_unavailable", "detail": metrics.get("outcome")}
        ranked = hook_lexical.apply_truth_eligibility(rows, svc)
        lines, rendered = context_hook._render_recall_lines(ranked, 2000, limit=hook_policy.max_claims())
        block = context_hook._recall_text(lines)
    finally:
        context_hook._REQUEST_SCOPE.reset(scope_token)
        context_hook._REQUEST_SHARED_SCOPES.reset(shared_token)
    pool = [{"cid": getattr(row["claim"], "human_id", None) or f"mm-{row['claim'].id}", "id": row["claim"].id,
             "body": row["claim"].text, "score": round(row.get("dense_score", 0.0), 4)} for row in ranked]
    return {
        "uuid": prompt["uuid"], "status": "ok", "pool": disambiguate(pool),
        "a_ids": [getattr(r["claim"], "id", None) for r in rendered], "a_chars": len(block),
        "indexed": metrics.get("indexed"), "above_threshold": metrics.get("above_threshold"),
    }


def disambiguate(pool: list[dict]) -> list[dict]:
    """Distinct claims can share a short id; inside one pool each gets ``<short>~<claim id>`` so reads resolve."""
    counts: dict[str, int] = {}
    for cand in pool:
        counts[cand["cid"]] = counts.get(cand["cid"], 0) + 1
    return [{**c, "cid": f"{c['cid']}~{c['id']}"} if counts[c["cid"]] > 1 else c for c in pool]


# ------------------------------------------------------------------------- models


def call_agy(model: str, prompt: str, work_dir: Path, timeout: int = 240) -> str:
    """One Gemini call through the repo's headless Antigravity client (OAuth, isolated batch profile)."""
    from memorymaster.core.antigravity_client import AntigravityClient

    return AntigravityClient(model=model, timeout=timeout, work_dir=work_dir).complete(prompt).text


def call_claude(model: str, prompt: str, work_dir: Path, timeout: int = 240) -> str:
    """One headless ``claude -p`` call that installed hooks must not recapture."""
    from memorymaster.core.llm_provider import _resolve_claude_bin
    from memorymaster.dreaming.providers import ClaudeCliExtractor, _default_command_runner

    exe = _resolve_claude_bin()
    if not exe:
        raise RuntimeError("claude CLI not on PATH")
    work_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "MEMORYMASTER_INTERNAL_RUN": "1", "MEMORYMASTER_DREAM_ENABLED": "0", "NO_COLOR": "1"}
    command = [exe, "-p", "--model", model, "--output-format", "json", "--no-session-persistence",
               "--tools", ""]
    done = _default_command_runner(command, prompt, timeout, work_dir, env)
    text, _, _, is_error = ClaudeCliExtractor._response(done.stdout or "")
    if done.returncode != 0 or is_error:
        raise RuntimeError(f"claude {model} exited {done.returncode}")
    return text


def _json_payload(raw: str) -> Any:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("no JSON in model output")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def reader_prompt(prompt: str, candidates: list[dict]) -> str:
    return (READER.format(max_reads=MAX_READS) + "\n\nUSER MESSAGE:\n" + mask_outgoing(prompt[:PROMPT_KEEP])
            + "\n\n" + mask_outgoing(index_text(candidates)))


def parse_reads(raw: str, candidates: list[dict]) -> list[str]:
    allowed = [c["cid"] for c in candidates]
    data = _json_payload(raw)
    picked = data.get("read", []) if isinstance(data, dict) else []
    out: list[str] = []
    for cid in picked if isinstance(picked, list) else []:
        if isinstance(cid, str) and cid in allowed and cid not in out:
            out.append(cid)
    return out[:MAX_READS]


def judge_order(uuid: str, pool: list[dict]) -> list[dict]:
    """Seeded shuffle so the judges never see the retrieval rank (A's claims are the first two)."""
    order = list(pool)
    random.Random(_sha(f"{SEED_PREFIX}:judge:{uuid}")).shuffle(order)
    return order


def judge_prompt(prompt: str, ordered: list[dict]) -> str:
    items = "\n\n".join(f"C{n}:\n{mask_outgoing(c['body'])}" for n, c in enumerate(ordered, 1))
    return f"{RUBRIC}\n\nUSER MESSAGE:\n{mask_outgoing(prompt[:PROMPT_KEEP])}\n\nCANDIDATES:\n{items}"


def parse_labels(raw: str, ordered: list[dict]) -> dict[str, str]:
    data = _json_payload(raw)
    labels: dict[str, str] = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        match = re.fullmatch(r"C(\d+)", str(item.get("c", "")).strip())
        label = str(item.get("label", "")).strip().upper()
        if match and label in LABELS and 1 <= int(match.group(1)) <= len(ordered):
            labels[ordered[int(match.group(1)) - 1]["cid"]] = label
    if len(labels) != len(ordered):
        raise ValueError(f"judge labelled {len(labels)}/{len(ordered)} candidates")
    return labels


# ------------------------------------------------------------------------ metrics


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    """Cohen's kappa for two binary raters."""
    n = len(pairs)
    if not n:
        return None
    observed = sum(a == b for a, b in pairs) / n
    pa = sum(a for a, _ in pairs) / n
    pb = sum(b for _, b in pairs) / n
    expected = pa * pb + (1 - pa) * (1 - pb)
    return round((observed - expected) / (1 - expected), 4) if expected < 1 else None


def consensus(labels: list[dict[str, str]]) -> dict[str, str]:
    """R or I only where every judge agrees; anything else is ``D`` (disputed) or ``U``."""
    out: dict[str, str] = {}
    for cid in labels[0]:
        values = {lab.get(cid) for lab in labels}
        if "U" in values:
            out[cid] = "U"
        elif len(values) == 1:
            out[cid] = values.pop()
        else:
            out[cid] = "D"
    return out


def strategy_metrics(events: list[dict], label_key: str) -> dict[str, Any]:
    """Aggregate A and B over events; ``label_key`` selects a judge's labels or the consensus.

    Each event: ``pool`` (list of candidates), ``a_ids`` (cids A injected), ``a_chars``,
    ``index_cids`` (cids in B's index), ``read_ids`` (cids B read), ``index_chars``, ``read_chars``
    and ``labels`` (dict label_key -> {cid: R|I|U|D}). Only R and I labels enter the relevance
    denominators; U and D are counted apart.
    """
    m = {k: 0 for k in ("events", "a_chars", "b_index_chars", "b_read_chars", "a_bodies", "a_rel", "a_irr",
                        "b_titles", "b_title_irr", "b_title_rel", "b_reads", "b_read_rel", "b_read_irr",
                        "pool", "pool_rel", "pool_irr", "pool_other", "events_with_rel", "a_events_hit",
                        "b_events_hit", "b_events_reading")}
    for ev in events:
        labels = ev["labels"][label_key]
        lab = lambda cid: labels.get(cid)  # noqa: E731
        m["events"] += 1
        m["a_chars"] += ev["a_chars"]
        m["b_index_chars"] += ev["index_chars"]
        m["b_read_chars"] += ev["read_chars"]
        pool_cids = [c["cid"] for c in ev["pool"]]
        m["pool"] += len(pool_cids)
        rel = {cid for cid in pool_cids if lab(cid) == "R"}
        m["pool_rel"] += len(rel)
        m["pool_irr"] += sum(lab(cid) == "I" for cid in pool_cids)
        m["pool_other"] += sum(lab(cid) not in ("R", "I") for cid in pool_cids)
        m["a_bodies"] += len(ev["a_ids"])
        m["a_rel"] += sum(lab(cid) == "R" for cid in ev["a_ids"])
        m["a_irr"] += sum(lab(cid) == "I" for cid in ev["a_ids"])
        m["b_titles"] += len(ev["index_cids"])
        m["b_title_rel"] += sum(lab(cid) == "R" for cid in ev["index_cids"])
        m["b_title_irr"] += sum(lab(cid) == "I" for cid in ev["index_cids"])
        m["b_reads"] += len(ev["read_ids"])
        m["b_read_rel"] += sum(lab(cid) == "R" for cid in ev["read_ids"])
        m["b_read_irr"] += sum(lab(cid) == "I" for cid in ev["read_ids"])
        m["b_events_reading"] += bool(ev["read_ids"])
        if rel:
            m["events_with_rel"] += 1
            m["a_events_hit"] += bool(rel & set(ev["a_ids"]))
            m["b_events_hit"] += bool(rel & set(ev["read_ids"]))
    n = m["events"]
    a_judged = m["a_rel"] + m["a_irr"]
    read_judged = m["b_read_rel"] + m["b_read_irr"]
    title_judged = m["b_title_rel"] + m["b_title_irr"]
    return {
        **m,
        "a_chars_per_prompt": _ratio(m["a_chars"], n),
        "b_chars_per_prompt": _ratio(m["b_index_chars"] + m["b_read_chars"], n),
        "b_index_chars_per_prompt": _ratio(m["b_index_chars"], n),
        "b_read_chars_per_prompt": _ratio(m["b_read_chars"], n),
        "a_irrelevant_rate": _ratio(m["a_irr"], a_judged),
        "b_read_irrelevant_rate": _ratio(m["b_read_irr"], read_judged),
        "b_title_irrelevant_rate": _ratio(m["b_title_irr"], title_judged),
        "a_irrelevant_bodies_per_prompt": _ratio(m["a_irr"], n),
        "b_irrelevant_bodies_per_prompt": _ratio(m["b_read_irr"], n),
        "a_relevant_recall": _ratio(m["a_rel"], m["pool_rel"]),
        "b_relevant_recall": _ratio(m["b_read_rel"], m["pool_rel"]),
        "a_prompt_hit_rate": _ratio(m["a_events_hit"], m["events_with_rel"]),
        "b_prompt_hit_rate": _ratio(m["b_events_hit"], m["events_with_rel"]),
    }


def bootstrap_diff(events: list[dict], label_key: str, rounds: int = 2000) -> dict[str, list[float | None]]:
    """95% percentile interval of B minus A, resampling prompts (the unit that repeats) with a fixed seed."""
    rng = random.Random(f"{SEED_PREFIX}:bootstrap:{label_key}")
    fields = ("chars_per_prompt", "irrelevant_bodies_per_prompt", "relevant_recall")
    draws: dict[str, list[float]] = {f: [] for f in fields}
    for _ in range(rounds):
        m = strategy_metrics([rng.choice(events) for _ in events], label_key)
        for f in fields:
            a, b = m[f"a_{f}"], m[f"b_{f}"]
            if a is not None and b is not None:
                draws[f].append(b - a)
    out: dict[str, list[float | None]] = {}
    for f, values in draws.items():
        values.sort()
        out[f] = [round(values[int(0.025 * len(values))], 4), round(values[int(0.975 * len(values)) - 1], 4)] \
            if values else [None, None]
    return out


# ---------------------------------------------------------------------- pipeline


def _paths(out: Path) -> dict[str, Path]:
    return {name: out / f"{name}.jsonl" for name in ("manifest", "pools", "reads", "judge_gemini", "judge_claude")}


def cmd_collect(args) -> None:
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    until = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
    sample, stats = collect(Path(args.projects).expanduser(), since, until, args.n, args.seed)
    path = _paths(out)["manifest"]
    path.write_text("".join(json.dumps(p, ensure_ascii=False, sort_keys=True) + "\n" for p in sample), encoding="utf-8")
    stats.update({"since": since.isoformat(), "until": until.isoformat(), "seed": args.seed})
    (out / "collect_stats.json").write_text(json.dumps(stats, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(stats, sort_keys=True))


def cmd_pool(args) -> None:
    from memorymaster.core.service import MemoryService

    out = Path(args.out_dir)
    paths = _paths(out)
    os.environ["MEMORYMASTER_RECALL_DENSE_MAX_CLAIMS"] = str(POOL_K)  # this process only: the full pool
    done = {row["uuid"] for row in _read_jsonl(paths["pools"])}
    svc = MemoryService(db_target=str(Path(args.snapshot)), workspace_root=Path.cwd(), read_only=True)
    for prompt in _read_jsonl(paths["manifest"]):
        if prompt["uuid"] not in done:
            _append_jsonl(paths["pools"], pool_for(prompt, svc))
    print(f"pools: {len(_read_jsonl(paths['pools']))}")


def _parallel(jobs, fn, workers: int) -> None:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(fn, jobs):
            if result:
                print(result, flush=True)


def _ok_pools(out: Path) -> list[tuple[dict, dict]]:
    paths = _paths(out)
    prompts = {p["uuid"]: p for p in _read_jsonl(paths["manifest"])}
    return [(prompts[p["uuid"]], p) for p in _read_jsonl(paths["pools"]) if p["status"] == "ok" and p["pool"]]


def cmd_read(args) -> None:
    out = Path(args.out_dir)
    path = _paths(out)["reads"]
    done = {row["uuid"] for row in _read_jsonl(path)}

    def run(item):
        prompt, pool = item
        if pool["uuid"] in done:
            return None
        candidates = pool["pool"][: args.index_k]
        try:
            raw = call_claude(args.reader_model, reader_prompt(prompt["prompt"], candidates), out / "work")
            reads = parse_reads(raw, candidates)
        except Exception as exc:  # noqa: BLE001 - a failed call is retried on the next run, never scored empty
            return f"read {pool['uuid'][:8]} failed: {exc}"
        _append_jsonl(path, {"uuid": pool["uuid"], "index_k": args.index_k, "reader": args.reader_model,
                             "read": reads})
        return None

    _parallel(_ok_pools(out), run, args.workers)
    print(f"reads: {len(_read_jsonl(path))}")


def cmd_judge(args) -> None:
    out = Path(args.out_dir)
    judges = {"judge_gemini": (call_agy, GEMINI_JUDGE), "judge_claude": (call_claude, CLAUDE_JUDGE)}
    judges = {name: judge for name, judge in judges.items() if args.judge in ("both", name.split("_")[1])}

    for name, (caller, model) in judges.items():
        path = _paths(out)[name]
        done = {row["uuid"] for row in _read_jsonl(path)}

        def run(item, caller=caller, model=model, path=path, done=done):
            prompt, pool = item
            if pool["uuid"] in done:
                return None
            ordered = judge_order(pool["uuid"], pool["pool"])
            last = None
            for _ in range(2):
                try:
                    labels = parse_labels(caller(model, judge_prompt(prompt["prompt"], ordered), out / "work"), ordered)
                    _append_jsonl(path, {"uuid": pool["uuid"], "model": model, "labels": labels})
                    return None
                except Exception as exc:  # noqa: BLE001 - retried once, then left for the next run
                    last = exc
            return f"{name} {pool['uuid'][:8]} failed: {last}"

        _parallel(_ok_pools(out), run, args.workers)
        print(f"{name}: {len(_read_jsonl(path))}")


def build_events(out: Path, index_k: int) -> tuple[list[dict], dict]:
    paths = _paths(out)
    reads = {r["uuid"]: r for r in _read_jsonl(paths["reads"])}
    gem = {r["uuid"]: r["labels"] for r in _read_jsonl(paths["judge_gemini"])}
    cla = {r["uuid"]: r["labels"] for r in _read_jsonl(paths["judge_claude"])}
    pools = _read_jsonl(paths["pools"])
    events, skipped = [], {"dense_unavailable": 0, "empty_pool": 0, "incomplete": 0}
    for pool in pools:
        if pool["status"] != "ok":
            skipped["dense_unavailable"] += 1
            continue
        if not pool["pool"]:
            skipped["empty_pool"] += 1
            continue
        uuid = pool["uuid"]
        if uuid not in reads or uuid not in gem or uuid not in cla:
            skipped["incomplete"] += 1
            continue
        id_to_cid = {c["id"]: c["cid"] for c in pool["pool"]}
        candidates = pool["pool"][:index_k]
        read_ids = reads[uuid]["read"]
        events.append({
            "uuid": uuid, "pool": pool["pool"], "a_ids": [id_to_cid[i] for i in pool["a_ids"] if i in id_to_cid],
            "a_chars": pool["a_chars"], "index_cids": [c["cid"] for c in candidates],
            "index_chars": len(index_text(candidates)), "read_ids": read_ids,
            "read_chars": len(read_text(candidates, read_ids)),
            "labels": {"gemini": gem[uuid], "claude": cla[uuid], "consensus": consensus([gem[uuid], cla[uuid]])},
        })
    return events, {"pools": len(pools), **skipped}


def cmd_report(args) -> None:
    out = Path(args.out_dir)
    events, skipped = build_events(out, args.index_k)
    pools = [p for p in _read_jsonl(_paths(out)["pools"]) if p["status"] == "ok"]
    # Prompts with an empty pool cost both strategies zero characters; they dilute per-prompt means.
    empty = sum(1 for p in pools if not p["pool"])
    pairs = [(e["labels"]["gemini"][c["cid"]] == "R", e["labels"]["claude"][c["cid"]] == "R")
             for e in events for c in e["pool"]]
    manifest = [{k: p[k] for k in ("uuid", "ts", "cwd")} for p in _read_jsonl(_paths(out)["manifest"])]
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "collect": json.loads((out / "collect_stats.json").read_text(encoding="utf-8")),
        "skipped": skipped, "empty_pool_prompts": empty, "index_k": args.index_k,
        "judges": {"gemini": GEMINI_JUDGE, "claude": CLAUDE_JUDGE}, "reader": CLAUDE_READER,
        "indexed": sorted({p.get("indexed") for p in pools if p.get("indexed") is not None}),
        "manifest_sha256": _sha(json.dumps(manifest, sort_keys=True, separators=(",", ":"))),
        "kappa_relevant": kappa(pairs), "raw_agreement": _ratio(sum(a == b for a, b in pairs), len(pairs)),
        "per_label_key": {key: strategy_metrics(events, key) for key in ("consensus", "gemini", "claude")},
        "bootstrap_b_minus_a": {key: bootstrap_diff(events, key) for key in ("consensus", "gemini", "claude")},
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    ledger = ["| Sample | Pool | A | Read | Gemini labels | Claude labels | A chars | Index chars | Read chars |",
              "|---|---:|---|---|---|---|---:|---:|---:|"]
    for n, e in enumerate(sorted(events, key=lambda e: _sha(f"{SEED_PREFIX}:ledger:{e['uuid']}")), 1):
        order = [c["cid"] for c in e["pool"]]
        pos = lambda ids: ",".join(str(order.index(i) + 1) for i in ids) or "-"  # noqa: E731
        gem = "".join(e["labels"]["gemini"][c] for c in order)
        cla = "".join(e["labels"]["claude"][c] for c in order)
        ledger.append(f"| S{n:02d} | {len(order)} | {pos(e['a_ids'])} | {pos(e['read_ids'])} | {gem} | {cla} | "
                      f"{e['a_chars']} | {e['index_chars']} | {e['read_chars']} |")
    (out / "ledger.md").write_text("\n".join(ledger) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("collect", "pool", "read", "judge", "report"):
        p = sub.add_parser(name)
        p.add_argument("--out-dir", required=True)
        p.add_argument("--index-k", type=int, default=8)
        p.add_argument("--workers", type=int, default=3)
        p.add_argument("--reader-model", default=CLAUDE_READER)
    sub.choices["collect"].add_argument("--projects", default=str(Path.home() / ".claude" / "projects"))
    sub.choices["collect"].add_argument("--since", required=True)
    sub.choices["collect"].add_argument("--until", required=True)
    sub.choices["collect"].add_argument("--n", type=int, default=80)
    sub.choices["collect"].add_argument("--seed", default="20261030")
    sub.choices["pool"].add_argument("--snapshot", required=True)
    sub.choices["judge"].add_argument("--judge", choices=("both", "gemini", "claude"), default="both")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    {"collect": cmd_collect, "pool": cmd_pool, "read": cmd_read, "judge": cmd_judge, "report": cmd_report}[args.cmd](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
