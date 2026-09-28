"""Measure one governed recall journey on a disposable SQLite database.

The evaluator records the production prompt-hook stages without ranking twice:
authorized candidate rows, the rows passed to the renderer, rendered IDs and
the actual delivery envelope. It never opens the authoritative database and it
does not call a remote or paid provider. The fixture labels are declarative
expectations, not ground-truth labels from a model.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import math
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import tomllib
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# A source-root override is useful for comparing an untouched checkout with a
# candidate worktree. It must be applied before any MemoryMaster import; the
# evaluator itself stays identical while only the implementation import path
# changes.
_SOURCE_ROOT = os.environ.get("MEMORYMASTER_EVAL_SOURCE_ROOT", "").strip()
_INSTALLED_MODE = (
    os.environ.get("MEMORYMASTER_EVAL_INSTALLED", "") == "1"
    or "--installed" in sys.argv
)
ROOT = Path(__file__).resolve().parents[1]


def _configure_import_path(source_root: str, installed_mode: bool) -> None:
    """Select source imports without putting site-packages ahead of stdlib."""
    if not installed_mode:
        sys.path.insert(0, source_root or str(ROOT))


_configure_import_path(_SOURCE_ROOT, _INSTALLED_MODE)

import memorymaster  # noqa: E402
from memorymaster.capture import CaptureRepository  # noqa: E402
from memorymaster.core.lifecycle import transition_claim  # noqa: E402
from memorymaster.core.models import CitationInput  # noqa: E402
from memorymaster.core.service import MemoryService  # noqa: E402
from memorymaster.recall import context_hook, delivery  # noqa: E402

DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "recall_journey_v1.json"

_OFF_ENV = {
    "MEMORYMASTER_JEV_MODE": "off",
    "MEMORYMASTER_JEV_RECALL": "0",
    "MEMORYMASTER_JEV_SESSION": "0",
    "MEMORYMASTER_DECISIONS_MODE": "off",
    "MEMORYMASTER_RECALL_RERANK_LOCAL": "0",
    "MEMORYMASTER_LLM_RERANK": "0",
    "MEMORYMASTER_RECALL_VERBATIM": "0",
    "MEMORYMASTER_RECALL_GRAPH_MODE": "off",
    "MEMORYMASTER_RECALL_GRAPH": "0",
    "MEMORYMASTER_RECALL_CLOSETS": "0",
    "MEMORYMASTER_RECALL_QUERY_EXPANSION": "0",
    "MEMORYMASTER_RECALL_VECTOR_FALLBACK": "0",
    "MEMORYMASTER_EMBEDDING_PROVIDER": "hash",
    "MEMORYMASTER_SCOPE_DEFAULT": "project:memorymaster",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _hermetic_environment(state_dir: Path):
    names = set(_OFF_ENV) | {
        "MEMORYMASTER_DEFAULT_DB", "MEMORYMASTER_DECISIONS_DB",
        "MEMORYMASTER_RECALL_STATE_DIR", "MEMORYMASTER_RECALL_LOG_DIR",
    }
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name, value in _OFF_ENV.items():
            os.environ[name] = value
        os.environ["MEMORYMASTER_RECALL_STATE_DIR"] = str(state_dir / "delivery")
        os.environ["MEMORYMASTER_RECALL_LOG_DIR"] = str(state_dir / "logs")
        os.environ["MEMORYMASTER_DECISIONS_DB"] = str(state_dir / "decisions.db")
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def _network_block(provider_calls: list[dict[str, Any]]):
    """Fail any accidental socket egress and make it visible in the report."""
    original_socket = socket.socket
    original_create = socket.create_connection

    class BlockedSocket(original_socket):
        def connect(self, address):  # type: ignore[override]
            provider_calls.append({"kind": "network", "blocked": True})
            raise RuntimeError("network blocked by recall journey evaluator")

    def blocked_create_connection(*args, **kwargs):
        provider_calls.append({"kind": "network", "blocked": True})
        raise RuntimeError("network blocked by recall journey evaluator")

    socket.socket = BlockedSocket  # type: ignore[assignment]
    socket.create_connection = blocked_create_connection  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket = original_socket
        socket.create_connection = original_create


def _load_fixture(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1:
        raise ValueError("unsupported recall journey fixture version")
    return payload


def _seed_claims(service: MemoryService, fixture: dict[str, Any]) -> dict[str, int]:
    ids: dict[str, int] = {}
    external = None
    for spec in fixture["claims"]:
        scope = str(spec.get("scope") or fixture["scope"])
        text = str(spec.get("text") or "")
        if spec.get("text_prefix") is not None:
            text = str(spec["text_prefix"]) * int(spec.get("text_repeat") or 1)
        if spec.get("text_suffix") is not None:
            text += str(spec["text_suffix"])
        if not text.strip():
            raise ValueError(f"fixture claim {spec['key']} has no text")
        claim = service.ingest(
            text=text,
            citations=[CitationInput(
                source=str(spec["citation_source"]),
                locator=f"claim:{spec['key']}",
                excerpt=text,
            )],
            claim_type="fact",
            scope=scope,
            confidence=0.9,
            source_agent="recall-journey-fixture",
            visibility=str(spec.get("visibility") or "public"),
        )
        ids[str(spec["key"])] = int(claim.id)
        source_to_retire = None
        if spec.get("retire_support"):
            if external is None:
                external = service.store.upsert_external_source(
                    source_type="recall-journey-fixture", display_name="retired support"
                )
            source_to_retire = service.store.upsert_source_item(
                source_id=external.id,
                source_item_id=f"support:{spec['key']}",
                item_type="text",
                text=text,
                sensitivity="none",
            )
            evidence = service.store.add_evidence_item(
                source_item_id=source_to_retire.id,
                evidence_type="text",
                text=text,
                sensitivity="none",
            )
            CaptureRepository(service.store).link_claim_evidence(
                claim_id=claim.id, evidence_item_id=evidence.id
            )
        status = str(spec.get("status") or "candidate")
        if status == "confirmed":
            claim = transition_claim(service.store, claim.id, "confirmed", "fixture promotion")
        elif status == "stale":
            claim = transition_claim(service.store, claim.id, "confirmed", "fixture promotion")
            claim = transition_claim(service.store, claim.id, "stale", "fixture retirement")
        elif status == "archived":
            claim = transition_claim(service.store, claim.id, "confirmed", "fixture promotion")
            claim = transition_claim(service.store, claim.id, "archived", "fixture retirement")
        elif status != "candidate":
            raise ValueError(f"unsupported fixture status: {status}")
        if source_to_retire is not None:
            # Exercise the public retirement path. It retires the source and,
            # when it was the sole active support, transitions a confirmed
            # claim to stale. This is a real lineage state, not a SQL unlink.
            from memorymaster.public.v1 import forget

            forget(
                source_item_id=source_to_retire.id,
                apply=True,
                db=service.store.db_path,
                workspace=Path(service.store.db_path).parent,
            )
    return ids


def _ids(rows: list[dict[str, Any]]) -> list[int]:
    out: list[int] = []
    for row in rows:
        claim = row.get("claim") if isinstance(row, dict) else None
        claim_id = getattr(claim, "id", None)
        if isinstance(claim_id, int):
            out.append(claim_id)
    return out


def _source_map(service: MemoryService, ids: list[int]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for claim_id in ids:
        claim = service.store.get_claim(claim_id, include_citations=True)
        if claim is not None:
            result[str(claim_id)] = [str(c.source) for c in claim.citations]
    return result


def _expected_texts(fixture: dict[str, Any], keys: list[str]) -> list[str]:
    by_key = {}
    for item in fixture["claims"]:
        text = str(item.get("text") or "")
        if item.get("text_prefix") is not None:
            text = str(item["text_prefix"]) * int(item.get("text_repeat") or 1)
        if item.get("text_suffix") is not None:
            text += str(item["text_suffix"])
        by_key[str(item["key"])] = text
    return [by_key[key] for key in keys]


def _citation_check(
    rendered_ids: list[int], citations: dict[str, list[str]], source_by_id: dict[str, str],
) -> tuple[int, int, bool | None]:
    checked = len(rendered_ids)
    correct = sum(
        bool(citations.get(str(claim_id)))
        and all(source == source_by_id.get(str(claim_id)) for source in citations[str(claim_id)])
        for claim_id in rendered_ids
    )
    return checked, correct, (correct == checked if checked else None)


def _nearest_rank_p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _run_case(
    fixture: dict[str, Any], ids: dict[str, int], service: MemoryService,
    state_dir: Path, case: dict[str, Any], provider_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_keys = [str(key) for key in case.get("expected", [])]
    forbidden_keys = [str(key) for key in case.get("forbidden", [])]
    expected_ids = [ids[key] for key in expected_keys]
    forbidden_ids = [ids[key] for key in forbidden_keys]
    budget_tokens = int(case.get("budget_tokens", 160))
    trace: dict[str, list[list[int]]] = {"candidates": [], "ranked": []}
    original_candidates = context_hook._governed_prompt_rows
    original_render = context_hook._render_recall_lines

    def capture_candidates(svc, query_text, *, limit):
        rows = original_candidates(svc, query_text, limit=limit)
        trace["candidates"].append(_ids(rows))
        return rows

    def capture_render(rows, budget, labels=None):
        trace["ranked"].append(_ids(rows))
        return original_render(rows, budget, labels)

    context_hook._governed_prompt_rows = capture_candidates
    context_hook._render_recall_lines = capture_render
    started = time.perf_counter()
    try:
        markdown, rendered_ids = context_hook.recall(
            str(case["query"]),
            db_path=str(service.store.db_path),
            budget=budget_tokens,
            skip_qdrant=True,
            return_ids=True,
        )
        output: list[str] = []
        delivery_data = {
            "session_id": f"recall-journey-{case['key']}",
            "cwd": "memorymaster",
        }
        delivered = delivery.deliver(
            delivery_data,
            delivery.recall_block(markdown),
            output.append,
            state_dir=state_dir / "delivery",
            now=1000.0,
        )
        decoded = json.loads(output[-1]) if output else {}
        additional = str(decoded.get("hookSpecificOutput", {}).get("additionalContext", ""))
    except Exception as exc:  # report case-level errors without hiding them
        return {
            "key": case["key"],
            "query": case["query"],
            "expected_ids": expected_ids,
            "forbidden_ids": forbidden_ids,
            "ranked_hit_at_5": False if expected_ids else None,
            "rendered_hit_at_5": False if expected_ids else None,
            "candidate_coverage": False if expected_ids else None,
            "ranked_precision_at_5_fixed": 0.0 if expected_ids else None,
            "rendered_precision_at_5_fixed": 0.0 if expected_ids else None,
            "expected_answer_present_in_decoded_delivery": False if expected_ids else None,
            "citation_checked_claim_count": 0,
            "citation_source_correct_count": 0,
            "citation_source_correct": None,
            "budget_tokens": budget_tokens,
            "budget_chars_estimate": budget_tokens * 4,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        context_hook._governed_prompt_rows = original_candidates
        context_hook._render_recall_lines = original_render
    elapsed = (time.perf_counter() - started) * 1000.0

    candidate_ids = list(dict.fromkeys(sum(trace["candidates"], [])))
    ranked_ids = trace["ranked"][-1] if trace["ranked"] else []
    ranked_hit = any(cid in ranked_ids[:5] for cid in expected_ids) if expected_ids else None
    rendered_hit = any(cid in rendered_ids[:5] for cid in expected_ids) if expected_ids else None
    candidate_coverage = any(cid in candidate_ids for cid in expected_ids) if expected_ids else None
    ranked_p_at_5 = (sum(cid in ranked_ids[:5] for cid in expected_ids) / 5
                     if expected_ids else None)
    rendered_p_at_5 = (sum(cid in rendered_ids[:5] for cid in expected_ids) / 5
                       if expected_ids else None)
    expected_texts = _expected_texts(fixture, expected_keys)
    answer_present = (all(text in additional for text in expected_texts)
                      if expected_texts else None)
    forbidden_excluded = all(cid not in rendered_ids for cid in forbidden_ids)
    citations = _source_map(service, rendered_ids)
    source_by_id = {
        str(ids[str(item["key"])]): str(item["citation_source"])
        for item in fixture["claims"]
    }
    citation_checked_claim_count, citation_source_correct_count, citation_source_correct = _citation_check(
        rendered_ids, citations, source_by_id,
    )
    overflow_ids = [ids[key] for key in case.get("allowed_overflow", [])]
    output_chars = len(additional)
    bullet_chars = sum(len(line) for line in additional.splitlines() if line.startswith("- "))
    return {
        "key": case["key"],
        "query": case["query"],
        "candidate_ids_by_token": trace["candidates"],
        "candidate_ids": candidate_ids,
        "ranked_ids": ranked_ids,
        "rendered_ids": rendered_ids,
        "expected_ids": expected_ids,
        "forbidden_ids": forbidden_ids,
        "ranked_hit_at_5": ranked_hit,
        "rendered_hit_at_5": rendered_hit,
        "candidate_coverage": candidate_coverage,
        "ranked_precision_at_5_fixed": ranked_p_at_5,
        "rendered_precision_at_5_fixed": rendered_p_at_5,
        "expected_answer_present_in_decoded_delivery": answer_present,
        "unicode_tail_present_in_decoded_delivery": "🎯" in additional,
        "forbidden_excluded": forbidden_excluded,
        "forbidden_in_candidates": any(
            cid in sum(trace["candidates"], []) for cid in forbidden_ids
        ),
        "forbidden_in_ranked": any(cid in ranked_ids for cid in forbidden_ids),
        "forbidden_in_rendered": any(cid in rendered_ids for cid in forbidden_ids),
        "allowed_overflow_in_candidates": any(cid in candidate_ids for cid in overflow_ids),
        "allowed_overflow_in_ranked": any(cid in ranked_ids for cid in overflow_ids),
        "allowed_overflow_rendered": any(cid in rendered_ids for cid in overflow_ids),
        "citation_sources_by_claim": citations,
        "citation_checked_claim_count": citation_checked_claim_count,
        "citation_source_correct_count": citation_source_correct_count,
        "citation_source_correct": citation_source_correct,
        "delivered": delivered,
        "additional_context_chars": output_chars,
        "rendered_bullet_chars": bullet_chars,
        "recall_context_chars": len(markdown),
        "recall_budget_compliant": len(markdown) <= budget_tokens * 4,
        "budget_tokens": budget_tokens,
        "budget_chars_estimate": budget_tokens * 4,
        "delivery_json": bool(decoded),
        "latency_ms": round(elapsed, 3),
    }


def run(fixture_path: Path, output_path: Path | None = None) -> dict[str, Any]:
    fixture = _load_fixture(fixture_path)
    provider_calls: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="memorymaster-recall-journey-") as raw_dir:
        state_dir = Path(raw_dir)
        with _hermetic_environment(state_dir), _network_block(provider_calls):
            db_path = state_dir / "journey.db"
            service = MemoryService(db_target=str(db_path), workspace_root=state_dir)
            service.init_db()
            ids = _seed_claims(service, fixture)
            cases = [_run_case(fixture, ids, service, state_dir, case, provider_calls)
                     for case in fixture["queries"]]
            # SQLiteStore currently hands out short-lived connections through
            # transaction context managers; they close on GC rather than on
            # context exit. Force collection before TemporaryDirectory removes
            # the disposable database on Windows, where an open handle blocks
            # unlink even after all SQL work has completed.
            del service
            gc.collect()
    latencies = [case["latency_ms"] for case in cases if "latency_ms" in case]
    p95 = _nearest_rank_p95(latencies)
    budget_estimates = [case["budget_chars_estimate"] for case in cases if "budget_chars_estimate" in case]
    budget_compliance = [case["recall_budget_compliant"] for case in cases if "recall_budget_compliant" in case]
    answer_cases = [case for case in cases if case.get("expected_ids")]
    script_path = Path(__file__).resolve()
    source_tree = None if _INSTALLED_MODE else Path(_SOURCE_ROOT or ROOT).resolve()
    imported_context_hook = Path(context_hook.__file__).resolve()
    imported_package_file = Path(memorymaster.__file__).resolve()
    package_root = imported_package_file.parent
    try:
        fixture_label = fixture_path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        fixture_label = fixture_path.name
    try:
        context_hook_label = imported_context_hook.relative_to(package_root.parent).as_posix()
    except ValueError:
        context_hook_label = imported_context_hook.name
    try:
        source_package_label = imported_package_file.relative_to(package_root.parent).as_posix()
    except ValueError:
        source_package_label = imported_package_file.name
    if source_tree is None:
        source_version = None
        git_head = None
    else:
        try:
            source_version = tomllib.loads(
                (source_tree / "pyproject.toml").read_text(encoding="utf-8")
            )["project"]["version"]
        except (KeyError, OSError, tomllib.TOMLDecodeError):
            source_version = None
        git_head = subprocess.run(
            ["git", "-C", str(source_tree), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
    distribution_version = importlib.metadata.version("memorymaster")
    result = {
        "schema": "memorymaster.recall_journey.v1",
        "fixture": fixture_label,
        "source_label": "installed_runtime" if _INSTALLED_MODE else ("source_override" if _SOURCE_ROOT else "candidate_worktree"),
        "fixture_sha256": _sha256(fixture_path),
        "evaluator_sha256": _sha256(script_path),
        "git_head": git_head,
        "source_context_hook": context_hook_label,
        "source_context_hook_sha256": _sha256(imported_context_hook),
        "source_package_file": source_package_label,
        "source_package_sha256": _sha256(imported_package_file),
        "pyproject_version": source_version,
        "distribution_version": distribution_version,
        "provider_calls_observed": provider_calls,
        "provider_call_count": len(provider_calls),
        "monetary_cost": "0 observed provider calls; no paid call made",
        "network_policy": "socket egress blocked during run",
        "sqlite_mode": "disposable temp database; authoritative database untouched",
        "jev_surfaces": "off",
        "cases": cases,
        "summary": {
            "cases_total": len(cases),
            "cases_without_error": sum("error" not in case for case in cases),
            "candidate_cases_total": len(answer_cases),
            "candidate_coverage": sum(case.get("candidate_coverage") is True for case in answer_cases) / len(answer_cases) if answer_cases else None,
            "ranked_hit_at_5": sum(case.get("ranked_hit_at_5") is True for case in answer_cases) / len(answer_cases) if answer_cases else None,
            "rendered_hit_at_5": sum(case.get("rendered_hit_at_5") is True for case in answer_cases) / len(answer_cases) if answer_cases else None,
            "ranked_precision_at_5_fixed": statistics.mean(case["ranked_precision_at_5_fixed"] for case in answer_cases) if answer_cases else None,
            "rendered_precision_at_5_fixed": statistics.mean(case["rendered_precision_at_5_fixed"] for case in answer_cases) if answer_cases else None,
            "answer_cases_total": len(answer_cases),
            "answer_present_rate": sum(
                case.get("expected_answer_present_in_decoded_delivery") is True
                for case in answer_cases
            ) / len(answer_cases) if answer_cases else None,
            "forbidden_exclusion_rate": sum(case.get("forbidden_excluded", False) for case in cases) / len(cases) if cases else None,
            "citation_checked_claims_total": sum(case.get("citation_checked_claim_count", 0) for case in cases),
            "citation_source_correct_rate": (
                sum(case.get("citation_source_correct_count", 0) for case in cases)
                / sum(case.get("citation_checked_claim_count", 0) for case in cases)
                if sum(case.get("citation_checked_claim_count", 0) for case in cases) else None
            ),
            "latency_p50_ms": statistics.median(latencies) if latencies else None,
            "latency_p95_ms": p95,
            "budget_chars_estimate_min": min(budget_estimates) if budget_estimates else None,
            "budget_chars_estimate_max": max(budget_estimates) if budget_estimates else None,
            "recall_budget_compliance_rate": (
                sum(budget_compliance) / len(budget_compliance) if budget_compliance else None
            ),
        },
        "limitations": [
            "Declarative fixture expectations are not independent human qrels.",
            "The fixture is small and lexical terms overlap by design; it measures governance and journey integrity, not production semantic quality.",
            "The evaluator does not claim the 953-prompt cohort or MiniLM quality result.",
            "Monetary cost is zero only for this run because provider_calls_observed is empty; no provider pricing was inferred.",
        ],
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument(
        "--source-root", type=Path, default=None,
        help="Import MemoryMaster from this checkout in a child process.",
    )
    parser.add_argument(
        "--installed", action="store_true",
        help="Run isolated from the interpreter's installed package without changing sys.path order.",
    )
    args = parser.parse_args()
    if args.source_root is not None and args.installed:
        parser.error("--source-root and --installed cannot be combined")
    if args.source_root is not None or args.installed:
        env = os.environ.copy()
        if args.installed:
            env.pop("MEMORYMASTER_EVAL_SOURCE_ROOT", None)
            env["MEMORYMASTER_EVAL_INSTALLED"] = "1"
            forwarded = [sys.executable, "-I", str(Path(__file__).resolve())]
        else:
            env.pop("MEMORYMASTER_EVAL_INSTALLED", None)
            env["MEMORYMASTER_EVAL_SOURCE_ROOT"] = str(args.source_root.resolve())
            forwarded = [sys.executable, str(Path(__file__).resolve())]
        forwarded.extend(["--fixture", str(args.fixture.resolve())])
        if args.json_out is not None:
            forwarded.extend(["--json-out", str(args.json_out.resolve())])
        return subprocess.run(forwarded, env=env, check=False).returncode
    result = run(args.fixture, args.json_out)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all("error" not in case for case in result["cases"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
