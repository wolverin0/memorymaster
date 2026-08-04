"""Run resumable LongMemEval OAuth QA chunks and aggregate exact evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "tests" / "bench_longmemeval.py"
API_KEY_NAMES = {"ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"}


def _read_json(path: Path) -> dict[str, Any] | list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _expected_ids(dataset: Path, expected_questions: int | None) -> list[str]:
    payload = _read_json(dataset)
    if not isinstance(payload, list):
        raise ValueError(f"dataset must be a JSON list: {dataset}")
    ids = [str(row["question_id"]) for row in payload]
    if expected_questions is not None and len(ids) != expected_questions:
        raise ValueError(
            f"dataset has {len(ids)} questions; expected {expected_questions}"
        )
    if len(ids) != len(set(ids)):
        raise ValueError("dataset contains duplicate question_id values")
    return ids


def _chunk_path(chunks_dir: Path, prefix: str, offset: int) -> Path:
    return chunks_dir / f"{prefix}-{offset:03d}.json"


def _load_valid_chunk(
    path: Path,
    expected_ids: list[str],
    *,
    judge_model: str,
    judge_effort: str,
) -> dict[str, Any]:
    wrapper = _read_json(path)
    if not isinstance(wrapper, dict) or not isinstance(wrapper.get("qa"), dict):
        raise ValueError(f"chunk is not a QA result: {path}")
    qa = wrapper["qa"]
    actual_ids = [str(row["question_id"]) for row in qa.get("results", [])]
    if qa.get("status") != "complete":
        raise ValueError(f"chunk status is {qa.get('status')!r}: {path}")
    if actual_ids != expected_ids:
        raise ValueError(f"chunk question IDs do not match its window: {path}")
    config = qa.get("judge_config") or {}
    if qa.get("judge_primary") != "opencode":
        raise ValueError(f"chunk did not use the OpenCode judge: {path}")
    if config.get("model") != judge_model or config.get("effort") != judge_effort:
        raise ValueError(f"chunk judge configuration does not match: {path}")
    return wrapper


def _build_command(
    *,
    python: Path,
    dataset: Path,
    retrieval_results: Path,
    output: Path,
    offset: int,
    limit: int,
    judge_model: str,
    judge_effort: str,
    max_seconds: int,
) -> list[str]:
    return [
        str(python),
        "-u",
        str(BENCHMARK),
        "--qa-only",
        "--dataset",
        str(dataset),
        "--output",
        str(retrieval_results),
        "--qa-output",
        str(output),
        "--offset",
        str(offset),
        "--limit",
        str(limit),
        "--judge",
        "opencode",
        "--judge-model",
        judge_model,
        "--judge-effort",
        judge_effort,
        "--judge-pacing-seconds",
        "0",
        "--qa-max-seconds",
        str(max_seconds),
    ]


def _archive_failed_chunk(path: Path, attempt: int) -> None:
    if not path.exists():
        return
    archived = path.with_name(f"{path.stem}.attempt-{attempt}.partial.json")
    if archived.exists():
        archived = path.with_name(
            f"{path.stem}.attempt-{attempt}-{time.time_ns()}.partial.json"
        )
    path.replace(archived)


def _run_chunk(
    command: list[str],
    *,
    output: Path,
    expected_ids: list[str],
    judge_model: str,
    judge_effort: str,
    max_attempts: int,
) -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if key not in API_KEY_NAMES}
    for attempt in range(1, max_attempts + 1):
        completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
        try:
            if completed.returncode != 0:
                raise RuntimeError(f"benchmark exited {completed.returncode}")
            return _load_valid_chunk(
                output,
                expected_ids,
                judge_model=judge_model,
                judge_effort=judge_effort,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            _archive_failed_chunk(output, attempt)
            if attempt == max_attempts:
                raise RuntimeError(
                    f"chunk failed after {max_attempts} attempts: {output}"
                ) from exc
            delay = min(60, 2 ** (attempt - 1))
            print(f"[qa-chunks] attempt {attempt} failed: {exc}; retrying in {delay}s")
            time.sleep(delay)
    raise AssertionError("unreachable")


def _aggregate(
    wrappers: list[tuple[Path, dict[str, Any]]],
    *,
    dataset: Path,
    retrieval_results: Path,
    expected_ids: list[str],
) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    chunks: list[dict[str, Any]] = []
    tokens = 0
    elapsed = 0.0
    for path, wrapper in wrappers:
        qa = wrapper["qa"]
        details.extend(qa["results"])
        provenance.extend(qa.get("judge_provenance", []))
        tokens += int(qa.get("tokens", 0))
        elapsed += float(qa.get("elapsed_seconds", 0.0))
        chunks.append({"path": path.name, "sha256": _sha256(path)})
        for row in qa["results"]:
            qtype = str(row.get("question_type") or "unknown")
            by_type[qtype]["total"] += 1
            by_type[qtype]["correct"] += int(bool(row["correct"]))
    actual_ids = [str(row["question_id"]) for row in details]
    if actual_ids != expected_ids:
        raise ValueError("aggregated question IDs do not exactly match the dataset")
    correct = sum(bool(row["correct"]) for row in details)
    first_qa = wrappers[0][1]["qa"]
    breakdown = {
        qtype: {
            "count": counts["total"],
            "correct": counts["correct"],
            "accuracy": counts["correct"] / counts["total"],
        }
        for qtype, counts in sorted(by_type.items())
    }
    qa = {
        **{key: first_qa[key] for key in ("mode", "judge_model", "judge_primary")},
        "judge_config": first_qa["judge_config"],
        "judge_provenance": provenance,
        "judge_retry_policy": first_qa.get("judge_retry_policy"),
        "judge_pacing_seconds": first_qa.get("judge_pacing_seconds"),
        "status": "complete",
        "questions": len(details),
        "requested_questions": len(expected_ids),
        "accuracy": correct / len(details),
        "correct": correct,
        "by_question_type": breakdown,
        "results": details,
        "elapsed_seconds": round(elapsed, 3),
        "tokens": tokens,
        "chunk_evidence": chunks,
        "dataset_sha256": _sha256(dataset),
        "retrieval_results_sha256": _sha256(retrieval_results),
    }
    return {"dataset": wrappers[0][1].get("dataset"), "status": "complete", "qa": qa}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--retrieval-results", type=Path, required=True)
    parser.add_argument("--chunks-dir", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--chunk-size", type=int, default=10)
    parser.add_argument("--expected-questions", type=int)
    parser.add_argument("--judge-model", default="openai/gpt-5.4-mini")
    parser.add_argument("--judge-effort", default="medium")
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--chunk-max-seconds", type=int, default=1800)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.chunk_size <= 0 or args.max_attempts <= 0:
        raise SystemExit("--chunk-size and --max-attempts must be greater than zero")
    expected_ids = _expected_ids(args.dataset, args.expected_questions)
    args.chunks_dir.mkdir(parents=True, exist_ok=True)
    wrappers: list[tuple[Path, dict[str, Any]]] = []
    for offset in range(0, len(expected_ids), args.chunk_size):
        chunk_ids = expected_ids[offset : offset + args.chunk_size]
        path = _chunk_path(args.chunks_dir, args.prefix, offset)
        try:
            wrapper = _load_valid_chunk(
                path,
                chunk_ids,
                judge_model=args.judge_model,
                judge_effort=args.judge_effort,
            )
            print(f"[qa-chunks] resumed verified chunk {offset}/{len(expected_ids)}")
        except (FileNotFoundError, OSError, ValueError):
            command = _build_command(
                python=args.python,
                dataset=args.dataset,
                retrieval_results=args.retrieval_results,
                output=path,
                offset=offset,
                limit=len(chunk_ids),
                judge_model=args.judge_model,
                judge_effort=args.judge_effort,
                max_seconds=args.chunk_max_seconds,
            )
            wrapper = _run_chunk(
                command,
                output=path,
                expected_ids=chunk_ids,
                judge_model=args.judge_model,
                judge_effort=args.judge_effort,
                max_attempts=args.max_attempts,
            )
        wrappers.append((path, wrapper))
        print(f"[qa-chunks] {min(offset + len(chunk_ids), len(expected_ids))}/{len(expected_ids)} durable")
    payload = _aggregate(
        wrappers,
        dataset=args.dataset,
        retrieval_results=args.retrieval_results,
        expected_ids=expected_ids,
    )
    _write_json_atomic(args.output, payload)
    qa = payload["qa"]
    print(
        f"[qa-chunks] complete accuracy={qa['accuracy']:.4f} "
        f"correct={qa['correct']}/{qa['questions']} tokens={qa['tokens']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
