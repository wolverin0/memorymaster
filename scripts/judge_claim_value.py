"""Batch LLM judge of the value of stored (confirmed) claims.

Answers "are the things MemoryMaster saves useful?" with a seeded random sample
of ``status=confirmed`` claims, each labelled useful / obvious / wrong / junk by
a Gemini batch judge, then aggregated overall, per source_agent and per scope.

Safety contract:
  * The source DB is only opened read-only (``mode=ro``) and copied once with the
    sqlite3 backup API into ``--out-dir``; every later read hits the snapshot.
  * Before anything leaves the machine each sampled claim goes through the repo
    sensitivity scan (``memorymaster.core.security``); flagged claims and claims
    with ``visibility=sensitive`` are dropped and counted. Home-directory paths
    and private IPv4 addresses in the remaining text are masked in the outgoing
    prompt only.
  * The API key is read at runtime (``GEMINI_API_KEY``, else the rotator key file
    ``~/.memorymaster/gemini-keys.env`` / ``MEMORYMASTER_KEY_FILE``), sent in the
    ``x-goog-api-key`` header, and never printed, logged or persisted.
  * Claim text is written only under ``--out-dir`` (outside the repo).

Usage:
    python scripts/judge_claim_value.py --db memorymaster.db --out-dir OUT --dry-run
    python scripts/judge_claim_value.py --db memorymaster.db --out-dir OUT
    python scripts/judge_claim_value.py --report --out-dir OUT [--human OUT/human_labels.json]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

RUBRIC_VERSION = "claim-value-rubric-v1"
VERDICTS = ("useful", "obvious", "wrong", "junk")
RUBRIC = f"""You are auditing the long-term memory store of a coding-agent system (rubric {RUBRIC_VERSION}).
Each item is one stored claim. Judge whether it is worth keeping, with exactly one verdict:
- useful: a non-obvious fact, decision, constraint or gotcha that an agent working in that project would act on.
- obvious: true, but derivable from the code, the docs or common knowledge, so storing it adds nothing.
- wrong: self-contradictory, stale by construction (e.g. a point-in-time status presented as lasting), or clearly false.
- junk: a routine action log, transcript noise, a fragment, or boilerplate.
Judge only the text given. When two verdicts fit, pick the one that is worse for the memory's value
(junk > wrong > obvious > useful). Answer with a JSON array, one object per item:
{{"n": <item number>, "verdict": "useful|obvious|wrong|junk", "reason": "<one short line>"}}"""

# Flash id the repo already uses (govern.llm_steward, recall.llm_rerank). The 3.5 ids used by dreaming
# timed out / returned 503 for this batch workload on 2026-10-13, so they are not the default.
DEFAULT_MODEL = "gemini-flash-latest"
MAX_TEXT_CHARS = 4500  # covers the longest claims seen; truncation would read as a fragment (junk)
MAX_ATTEMPTS_PER_CLAIM = 2
MIN_N_RANKED = 10
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 60.0

_HOME_PATH_RE = re.compile(r"(?i)\b[a-z]:[\\/]+users[\\/]+[^\\/\s\"']+|/(?:home|users)/[^/\s\"']+")
_PRIVATE_IP_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)

Transport = Callable[[str, dict[str, Any], dict[str, str], float], tuple[int, dict[str, Any]]]


class JudgeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- DB


def snapshot_db(src: Path, dest: Path) -> Path:
    """Copy ``src`` to ``dest`` with the sqlite3 backup API; the source is opened read-only."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dest


def sample_claims(snapshot: Path, n: int, seed: int) -> list[dict[str, Any]]:
    """Seeded random sample of confirmed claims (deterministic for a fixed snapshot+seed)."""
    conn = sqlite3.connect(f"{snapshot.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM claims WHERE status='confirmed' ORDER BY id")]
        chosen = sorted(random.Random(seed).sample(ids, min(n, len(ids))))
        rows: list[dict[str, Any]] = []
        for start in range(0, len(chosen), 500):
            part = chosen[start : start + 500]
            marks = ",".join("?" * len(part))
            rows.extend(
                dict(r)
                for r in conn.execute(
                    "SELECT id, text, claim_type, subject, predicate, object_value, scope, source_agent,"
                    f" created_at, visibility FROM claims WHERE id IN ({marks}) ORDER BY id",
                    part,
                )
            )
        return rows
    finally:
        conn.close()


# ------------------------------------------------------------------ sensitivity


def is_flagged(claim: dict[str, Any]) -> bool:
    """True when the claim must not leave the machine (sensitivity scan or sensitive visibility)."""
    from memorymaster.core.security import scan_text_for_findings

    if str(claim.get("visibility") or "").strip().lower() == "sensitive":
        return True
    combined = " ".join(
        str(claim.get(k) or "") for k in ("text", "subject", "predicate", "object_value") if claim.get(k)
    )
    if "[REDACTED:" in combined:
        return True
    return bool(scan_text_for_findings(combined))


def filter_sensitive(claims: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    kept = [c for c in claims if not is_flagged(c)]
    return kept, len(claims) - len(kept)


def mask_outgoing(text: str) -> str:
    """Mask home-directory paths and private IPv4 addresses in text bound for the API."""
    return _PRIVATE_IP_RE.sub("<private-ip>", _HOME_PATH_RE.sub("<home>", text))


# ----------------------------------------------------------------------- Gemini


def load_keys(env: dict[str, str] | None = None, key_file: Path | None = None) -> list[str]:
    """Runtime key list: ``GEMINI_API_KEY`` first, else the repo's rotator key file. Never logged."""
    env = os.environ if env is None else env
    single = (env.get("GEMINI_API_KEY") or "").strip()
    if single:
        return [single]
    from memorymaster.core import key_rotator

    path = key_file or (
        Path(env["MEMORYMASTER_KEY_FILE"]) if env.get("MEMORYMASTER_KEY_FILE") else key_rotator.DEFAULT_KEY_FILE
    )
    return [slot.value for slot in key_rotator._parse_file(path)]


def default_transport(
    url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https endpoint
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            body = {}
        return exc.code, body if isinstance(body, dict) else {}


def build_prompt(batch: list[dict[str, Any]]) -> str:
    items = []
    for number, claim in enumerate(batch, start=1):
        body = mask_outgoing(str(claim.get("text") or ""))[:MAX_TEXT_CHARS]
        items.append(
            {
                "n": number,
                "scope": claim.get("scope"),
                "claim_type": claim.get("claim_type"),
                "created_at": str(claim.get("created_at") or "")[:10],
                "text": body,
            }
        )
    return f"{RUBRIC}\n\nItems:\n{json.dumps(items, ensure_ascii=False)}"


def thinking_config(model: str) -> dict[str, Any]:
    """Keep judging cheap: no thinking budget on 2.5, minimal level on 3.x."""
    return {"thinkingBudget": 0} if "2.5" in model else {"thinkingLevel": "minimal"}


def build_payload(batch: list[dict[str, Any]], model: str = DEFAULT_MODEL) -> dict[str, Any]:
    schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "required": ["n", "verdict", "reason"],
            "properties": {
                "n": {"type": "INTEGER"},
                "verdict": {"type": "STRING", "enum": list(VERDICTS)},
                "reason": {"type": "STRING"},
            },
        },
    }
    return {
        "contents": [{"parts": [{"text": build_prompt(batch)}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 8000,
            "thinkingConfig": thinking_config(model),
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }


def parse_verdicts(raw: str, batch_size: int) -> dict[int, tuple[str, str]]:
    """Parse a judge reply into ``{item number: (verdict, reason)}``.

    Tolerates code fences and prose around the array; ignores malformed or
    out-of-range entries. Returns ``{}`` when nothing usable is found.
    """
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    data: Any = None
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except ValueError:
                data = None
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                data = value
                break
    if not isinstance(data, list):
        return {}
    out: dict[int, tuple[str, str]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            number = int(entry.get("n"))
        except (TypeError, ValueError):
            continue
        verdict = str(entry.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS or not 1 <= number <= batch_size:
            continue
        out[number] = (verdict, str(entry.get("reason") or "").strip()[:240])
    return out


def response_text(body: dict[str, Any]) -> str:
    try:
        parts = body["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return ""
    return "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))


class Judge:
    """Gemini REST batch judge with a hard cap on HTTP requests."""

    def __init__(
        self,
        keys: list[str],
        *,
        model: str = DEFAULT_MODEL,
        max_requests: int = 40,
        transport: Transport = default_transport,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_s: float = 0.0,
    ) -> None:
        if not keys:
            raise JudgeError("no Gemini API key available (set GEMINI_API_KEY or the rotator key file)")
        self._keys = list(keys)
        self._key_index = 0
        self.model = model
        self.max_requests = max_requests
        self.requests = 0
        self._transport = transport
        self._sleep = sleep
        self._min_interval_s = min_interval_s

    @property
    def url(self) -> str:
        return f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """POST with exponential backoff on 429/5xx; every HTTP attempt counts against the cap."""
        delay = BACKOFF_BASE_S
        while self.requests < self.max_requests:
            headers = {"Content-Type": "application/json", "x-goog-api-key": self._keys[self._key_index]}
            self.requests += 1
            started = time.monotonic()
            try:
                status, body = self._transport(self.url, payload, headers, 120.0)
            except (urllib.error.URLError, TimeoutError, OSError):
                status, body = 599, {}
            print(
                f"request {self.requests}/{self.max_requests} status={status} {time.monotonic() - started:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            if status == 200:
                if self._min_interval_s:
                    self._sleep(self._min_interval_s)  # stay under the per-minute request limit
                return body
            if status == 429 or status >= 500:
                if status == 429 and len(self._keys) > 1:
                    self._key_index = (self._key_index + 1) % len(self._keys)
                self._sleep(delay)
                delay = min(delay * 2, BACKOFF_CAP_S)
                continue
            raise JudgeError(f"Gemini request rejected with HTTP {status}")
        return None

    def judge_batch(self, batch: list[dict[str, Any]]) -> dict[int, tuple[str, str]] | None:
        """Verdicts for one batch (``{}`` on unusable reply); ``None`` when the request cap was hit."""
        body = self._post(build_payload(batch, self.model))
        if body is None:
            return None
        return parse_verdicts(response_text(body), len(batch))


def judge_all(
    claims: list[dict[str, Any]],
    judge: Judge,
    *,
    batch_size: int,
    on_verdict: Callable[[dict[str, Any], str, str], None],
) -> dict[str, int]:
    """Judge every claim in batches; claims missing from a reply are retried while the cap allows."""
    attempts: Counter[int] = Counter()
    pending = list(claims)
    while pending and judge.requests < judge.max_requests:
        batch = [c for c in pending if attempts[c["id"]] < MAX_ATTEMPTS_PER_CLAIM][:batch_size]
        if not batch:
            break
        for c in batch:
            attempts[c["id"]] += 1
        result = judge.judge_batch(batch)
        if result is None:
            break
        done: set[int] = set()
        for number, (verdict, reason) in result.items():
            claim = batch[number - 1]
            on_verdict(claim, verdict, reason)
            done.add(claim["id"])
        pending = [c for c in pending if c["id"] not in done]
    unjudged = [c for c in pending]
    return {"unjudged": len(unjudged), "requests": judge.requests}


# ----------------------------------------------------------------------- report


def _shares(counter: Counter[str]) -> dict[str, float]:
    total = sum(counter.values())
    return {v: round(counter.get(v, 0) / total, 4) if total else 0.0 for v in VERDICTS}


def _bad_share(counter: Counter[str]) -> float:
    total = sum(counter.values())
    return (counter["junk"] + counter["wrong"] + counter["obvious"]) / total if total else 0.0


def _group(rows: list[dict[str, Any]], key: str, min_n: int) -> list[dict[str, Any]]:
    groups: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        groups[str(row.get(key) or "(none)")][row["verdict"]] += 1
    out = []
    for name, counter in groups.items():
        n = sum(counter.values())
        out.append(
            {
                "name": name,
                "n": n,
                "counts": {v: counter.get(v, 0) for v in VERDICTS},
                "shares": _shares(counter),
                "bad_share": round(_bad_share(counter), 4),
                "ranked": n >= min_n,
            }
        )
    out.sort(key=lambda g: (not g["ranked"], -g["bad_share"], -g["n"], g["name"]))
    return out


def agreement(rows: list[dict[str, Any]], human: dict[str, str]) -> dict[str, Any]:
    by_id = {str(r["id"]): r["verdict"] for r in rows}
    pairs = [(human[k], by_id[k]) for k in human if k in by_id]
    if not pairs:
        return {"n": 0}
    exact = sum(1 for h, j in pairs if h == j)
    # Coarse view: is the claim worth keeping (useful) or not (obvious/wrong/junk)?
    coarse = sum(1 for h, j in pairs if (h == "useful") == (j == "useful"))
    confusion: Counter[str] = Counter(f"{h}->{j}" for h, j in pairs)
    return {
        "n": len(pairs),
        "exact": exact,
        "exact_share": round(exact / len(pairs), 4),
        "useful_vs_not": coarse,
        "useful_vs_not_share": round(coarse / len(pairs), 4),
        "confusion_human_to_judge": dict(sorted(confusion.items())),
    }


def build_summary(
    rows: list[dict[str, Any]], meta: dict[str, Any], *, min_n: int = MIN_N_RANKED, human: dict[str, str] | None = None
) -> dict[str, Any]:
    overall: Counter[str] = Counter(r["verdict"] for r in rows)
    agents = _group(rows, "source_agent", min_n)
    summary: dict[str, Any] = {
        **meta,
        "rubric_version": RUBRIC_VERSION,
        "judged": len(rows),
        "overall": {"counts": {v: overall.get(v, 0) for v in VERDICTS}, "shares": _shares(overall)},
        "by_source_agent": agents,
        "worst_source_agents": [g for g in agents if g["ranked"]][:5],
        "by_scope": _group(rows, "scope", min_n),
        "by_claim_type": _group(rows, "claim_type", min_n),
    }
    if human:
        summary["human_agreement"] = agreement(rows, human)
    return summary


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def format_report(summary: dict[str, Any]) -> str:
    def pct(s: dict[str, float]) -> str:
        return " ".join(f"{v}={s[v] * 100:.1f}%" for v in VERDICTS)

    lines = [
        f"judged={summary['judged']} model={summary.get('model')} rubric={summary['rubric_version']}",
        f"overall: {pct(summary['overall']['shares'])}",
        "worst source_agents (n>=%d, by junk+wrong+obvious):" % MIN_N_RANKED,
    ]
    for g in summary["worst_source_agents"]:
        lines.append(f"  {g['name']} n={g['n']} bad={g['bad_share'] * 100:.1f}% | {pct(g['shares'])}")
    lines.append("by scope:")
    for g in summary["by_scope"]:
        lines.append(f"  {g['name']} n={g['n']} bad={g['bad_share'] * 100:.1f}% | {pct(g['shares'])}")
    if "human_agreement" in summary:
        lines.append(f"human agreement: {json.dumps(summary['human_agreement'])}")
    return "\n".join(lines)


# ------------------------------------------------------------------------- main


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _load_human(path: Path | None) -> dict[str, str] | None:
    if path is None or not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.items() if str(v) in VERDICTS}


def run_report(out_dir: Path, human_path: Path | None) -> dict[str, Any]:
    rows = read_jsonl(out_dir / "verdicts.jsonl")
    meta_path = out_dir / "summary.json"
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        previous = json.loads(meta_path.read_text(encoding="utf-8"))
        meta = {
            k: previous[k]
            for k in ("model", "seed", "sampled", "dropped_sensitive", "requests", "unjudged")
            if k in previous
        }
    summary = build_summary(rows, meta, human=_load_human(human_path or out_dir / "human_labels.json"))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def run(
    args: argparse.Namespace, transport: Transport = default_transport, sleep: Callable[[float], None] = time.sleep
) -> dict[str, Any]:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.report:
        return run_report(out_dir, Path(args.human) if args.human else None)

    frozen = out_dir / "sample.jsonl"
    previous = _read_json(out_dir / "summary.json")
    if not args.dry_run and not args.resample and frozen.is_file() and "sampled" in previous:
        # A prior dry run froze the sample (and any blind labels were taken from it): judge exactly that set.
        claims = read_jsonl(frozen)
        meta = {k: previous[k] for k in ("seed", "sampled", "dropped_sensitive") if k in previous}
        meta.update({"model": args.model, "to_judge": len(claims), "sample_reused": True})
    else:
        snap = snapshot_db(Path(args.db), out_dir / "snapshot.db")
        try:
            sampled = sample_claims(snap, args.n, args.seed)
        finally:
            if not args.keep_snapshot:
                for leftover in (snap, snap.with_name(snap.name + "-wal"), snap.with_name(snap.name + "-shm")):
                    leftover.unlink(missing_ok=True)
        claims, dropped = filter_sensitive(sampled)
        _write_jsonl(frozen, claims)
        meta = {
            "model": args.model,
            "seed": args.seed,
            "sampled": len(sampled),
            "dropped_sensitive": dropped,
            "to_judge": len(claims),
        }
    if args.dry_run:
        meta["dry_run"] = True
        (out_dir / "summary.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

    verdicts_path = out_dir / "verdicts.jsonl"
    rows = read_jsonl(verdicts_path)
    seen = {r["id"] for r in rows}
    todo = [c for c in claims if c["id"] not in seen]
    judge = Judge(
        load_keys(),
        model=args.model,
        max_requests=args.max_requests,
        transport=transport,
        sleep=sleep,
        min_interval_s=args.min_interval,
    )
    with verdicts_path.open("a", encoding="utf-8") as sink:

        def record(claim: dict[str, Any], verdict: str, reason: str) -> None:
            row = {
                "id": claim["id"],
                "scope": claim["scope"],
                "claim_type": claim["claim_type"],
                "source_agent": claim["source_agent"],
                "created_at": claim["created_at"],
                "verdict": verdict,
                "reason": reason,
            }
            rows.append(row)
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            sink.flush()

        stats = judge_all(todo, judge, batch_size=args.batch, on_verdict=record)
    meta.update({"requests": stats["requests"], "unjudged": stats["unjudged"]})
    summary = build_summary(rows, meta, human=_load_human(out_dir / "human_labels.json"))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--n", type=int, default=400, help="sample size of confirmed claims")
    p.add_argument("--seed", type=int, default=20261013)
    p.add_argument("--db", default="memorymaster.db")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--max-requests", type=int, default=40, help="hard cap on HTTP requests (retries included)")
    p.add_argument("--batch", type=int, default=20, help="claims per judge request")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--dry-run", action="store_true", help="snapshot, sample and filter only; no network")
    p.add_argument("--report", action="store_true", help="aggregate an existing verdicts file; no network")
    p.add_argument("--human", help="JSON {claim_id: verdict} blind labels for the agreement check")
    p.add_argument("--min-interval", type=float, default=4.5, help="seconds to pause after each successful request")
    p.add_argument("--resample", action="store_true", help="ignore a frozen sample.jsonl and sample the DB again")
    p.add_argument("--keep-snapshot", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run(args)
    except JudgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if "overall" in summary:
        print(format_report(summary))
    else:
        print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
