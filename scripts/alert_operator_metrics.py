from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
        if math.isfinite(parsed):
            return parsed
    except (TypeError, ValueError):
        return None
    return None


def _parse_kv_thresholds(values: list[str], *, arg_name: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for raw in values:
        token = str(raw).strip()
        if not token:
            continue
        if "=" not in token:
            raise ValueError(f"{arg_name} expects operation=value, got: {token}")
        operation, raw_value = token.split("=", 1)
        op = operation.strip().lower()
        if not op:
            raise ValueError(f"{arg_name} operation is empty: {token}")
        threshold = _safe_float(raw_value.strip())
        if threshold is None:
            raise ValueError(f"{arg_name} threshold is not numeric: {token}")
        result[op] = float(threshold)
    return result


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"metrics JSON must be an object: {path}")
    return payload


def _resolve_webhook_url(cli_url: str, env_name: str) -> str:
    if str(cli_url).strip():
        return str(cli_url).strip()
    env_key = str(env_name).strip()
    if env_key:
        return str(os.getenv(env_key, "")).strip()
    return ""


def _derive_error_totals(metrics: dict[str, Any]) -> tuple[int, float]:
    errors = metrics.get("errors")
    if isinstance(errors, dict):
        total = _safe_int(errors.get("total"))
        rate = _safe_float(errors.get("error_rate"))
        return total, (float(rate) if rate is not None else 0.0)

    total_events = _safe_int(metrics.get("total_events"))
    state_error = _safe_int(metrics.get("state_error"))
    json_errors = _safe_int(metrics.get("json_errors"))
    stream_open = max(_safe_int(metrics.get("stream_starts")) - _safe_int(metrics.get("stream_exits")), 0)
    total_errors = state_error + json_errors + stream_open
    error_rate = (float(total_errors) / float(total_events)) if total_events > 0 else 0.0
    return total_errors, error_rate


def _derive_queue_max_backlog(metrics: dict[str, Any]) -> int:
    queue = metrics.get("queue")
    if isinstance(queue, dict):
        return max(0, _safe_int(queue.get("max_backlog")))
    seen = _safe_int(metrics.get("total_events"))
    processed = _safe_int(metrics.get("turns_processed"))
    return max(0, seen - processed)


def _check_latency_thresholds(
    *,
    metrics: dict[str, Any],
    p50_max_ms: dict[str, float],
    p95_max_ms: dict[str, float],
    require_samples: bool,
    checks: list[dict[str, Any]],
    breaches: list[dict[str, Any]],
) -> None:
    latency = metrics.get("latency_ms")
    if not isinstance(latency, dict):
        latency = {}

    for operation, threshold in sorted(p50_max_ms.items()):
        row = latency.get(operation)
        count = _safe_int(row.get("count")) if isinstance(row, dict) else 0
        value = _safe_float(row.get("p50_ms")) if isinstance(row, dict) else None
        if count <= 0 or value is None:
            if require_samples:
                breaches.append(
                    {
                        "kind": "latency_p50_missing",
                        "operation": operation,
                        "threshold_max_ms": threshold,
                        "actual": value,
                        "sample_count": count,
                    }
                )
            checks.append(
                {
                    "kind": "latency_p50",
                    "operation": operation,
                    "sample_count": count,
                    "threshold_max_ms": threshold,
                    "actual_ms": value,
                    "ok": (not require_samples),
                    "skipped": True,
                }
            )
            continue

        ok = value <= threshold
        checks.append(
            {
                "kind": "latency_p50",
                "operation": operation,
                "sample_count": count,
                "threshold_max_ms": threshold,
                "actual_ms": round(value, 3),
                "ok": ok,
                "skipped": False,
            }
        )
        if not ok:
            breaches.append(
                {
                    "kind": "latency_p50",
                    "operation": operation,
                    "threshold_max_ms": threshold,
                    "actual_ms": round(value, 3),
                    "sample_count": count,
                }
            )

    for operation, threshold in sorted(p95_max_ms.items()):
        row = latency.get(operation)
        count = _safe_int(row.get("count")) if isinstance(row, dict) else 0
        value = _safe_float(row.get("p95_ms")) if isinstance(row, dict) else None
        if count <= 0 or value is None:
            if require_samples:
                breaches.append(
                    {
                        "kind": "latency_p95_missing",
                        "operation": operation,
                        "threshold_max_ms": threshold,
                        "actual": value,
                        "sample_count": count,
                    }
                )
            checks.append(
                {
                    "kind": "latency_p95",
                    "operation": operation,
                    "sample_count": count,
                    "threshold_max_ms": threshold,
                    "actual_ms": value,
                    "ok": (not require_samples),
                    "skipped": True,
                }
            )
            continue

        ok = value <= threshold
        checks.append(
            {
                "kind": "latency_p95",
                "operation": operation,
                "sample_count": count,
                "threshold_max_ms": threshold,
                "actual_ms": round(value, 3),
                "ok": ok,
                "skipped": False,
            }
        )
        if not ok:
            breaches.append(
                {
                    "kind": "latency_p95",
                    "operation": operation,
                    "threshold_max_ms": threshold,
                    "actual_ms": round(value, 3),
                    "sample_count": count,
                }
            )


def _post_webhook(url: str, payload: dict[str, Any], *, timeout_seconds: float) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    request = Request(
        url=url,
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urlopen(request, timeout=max(1.0, float(timeout_seconds))) as response:
            code = int(getattr(response, "status", 0) or 0)
            if code < 200 or code >= 300:
                return False, f"unexpected_status:{code}"
            return True, f"status:{code}"
    except URLError as exc:
        return False, f"url_error:{exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"post_error:{exc}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alert gate for operator metrics thresholds.")
    parser.add_argument(
        "--metrics-json",
        default="artifacts/e2e/operator_metrics.json",
        help="Path to alert input metrics JSON.",
    )
    parser.add_argument(
        "--out-json",
        default="artifacts/e2e/operator_alert.json",
        help="Path to write alert decision JSON.",
    )
    parser.add_argument(
        "--queue-max",
        type=int,
        default=None,
        help="Maximum allowed queue backlog (max_backlog). Omit to disable.",
    )
    parser.add_argument(
        "--error-max",
        type=int,
        default=0,
        help="Maximum allowed total operator errors.",
    )
    parser.add_argument(
        "--error-rate-max",
        type=float,
        default=None,
        help="Maximum allowed total error rate (errors / total_events). Omit to disable.",
    )
    parser.add_argument(
        "--p50-max-ms",
        action="append",
        default=[],
        help="p50 threshold as operation=max_ms (repeatable, e.g. operator_turn=300).",
    )
    parser.add_argument(
        "--p95-max-ms",
        action="append",
        default=[],
        help="p95 threshold as operation=max_ms (repeatable, e.g. query=500).",
    )
    parser.add_argument(
        "--require-latency-samples",
        action="store_true",
        help="Fail if a configured latency threshold has no sample data.",
    )
    parser.add_argument(
        "--webhook-url",
        default="",
        help="Webhook URL for posting decision payload (optional).",
    )
    parser.add_argument(
        "--webhook-env",
        default="",
        help="Environment variable that contains webhook URL (used if --webhook-url is empty).",
    )
    parser.add_argument(
        "--webhook-timeout-seconds",
        type=float,
        default=5.0,
        help="Webhook POST timeout in seconds.",
    )
    parser.add_argument(
        "--fail-on-webhook-error",
        action="store_true",
        help="Return non-zero when webhook delivery fails.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    metrics_path = Path(args.metrics_json)
    out_path = Path(args.out_json)

    if not metrics_path.exists():
        report = {
            "timestamp": _utc_now_iso(),
            "status": "no_metrics",
            "passed": False,
            "metrics_json": str(metrics_path),
            "breaches": [
                {
                    "kind": "missing_metrics",
                    "path": str(metrics_path),
                }
            ],
        }
        _write_json(out_path, report)
        print(f"status=no_metrics out={out_path}")
        return 2

    metrics = _load_json(metrics_path)
    p50_max = _parse_kv_thresholds(list(args.p50_max_ms), arg_name="--p50-max-ms")
    p95_max = _parse_kv_thresholds(list(args.p95_max_ms), arg_name="--p95-max-ms")

    checks: list[dict[str, Any]] = []
    breaches: list[dict[str, Any]] = []

    queue_max = _derive_queue_max_backlog(metrics)
    if args.queue_max is not None:
        queue_ok = queue_max <= int(args.queue_max)
        checks.append(
            {
                "kind": "queue_max_backlog",
                "threshold_max": int(args.queue_max),
                "actual": queue_max,
                "ok": queue_ok,
            }
        )
        if not queue_ok:
            breaches.append(
                {
                    "kind": "queue_max_backlog",
                    "threshold_max": int(args.queue_max),
                    "actual": queue_max,
                }
            )

    error_total, error_rate = _derive_error_totals(metrics)
    error_total_ok = error_total <= int(args.error_max)
    checks.append(
        {
            "kind": "error_total",
            "threshold_max": int(args.error_max),
            "actual": error_total,
            "ok": error_total_ok,
        }
    )
    if not error_total_ok:
        breaches.append(
            {
                "kind": "error_total",
                "threshold_max": int(args.error_max),
                "actual": error_total,
            }
        )

    if args.error_rate_max is not None:
        error_rate_ok = error_rate <= float(args.error_rate_max)
        checks.append(
            {
                "kind": "error_rate",
                "threshold_max": float(args.error_rate_max),
                "actual": round(error_rate, 6),
                "ok": error_rate_ok,
            }
        )
        if not error_rate_ok:
            breaches.append(
                {
                    "kind": "error_rate",
                    "threshold_max": float(args.error_rate_max),
                    "actual": round(error_rate, 6),
                }
            )

    _check_latency_thresholds(
        metrics=metrics,
        p50_max_ms=p50_max,
        p95_max_ms=p95_max,
        require_samples=bool(args.require_latency_samples),
        checks=checks,
        breaches=breaches,
    )

    passed = len(breaches) == 0
    webhook_url = _resolve_webhook_url(args.webhook_url, args.webhook_env)
    report: dict[str, Any] = {
        "timestamp": _utc_now_iso(),
        "status": "ok" if passed else "breach",
        "passed": passed,
        "metrics_json": str(metrics_path),
        "thresholds": {
            "queue_max": args.queue_max,
            "error_max": int(args.error_max),
            "error_rate_max": args.error_rate_max,
            "p50_max_ms": p50_max,
            "p95_max_ms": p95_max,
            "require_latency_samples": bool(args.require_latency_samples),
        },
        "summary": {
            "queue_max_backlog": queue_max,
            "error_total": error_total,
            "error_rate": round(error_rate, 6),
            "checks": len(checks),
            "breaches": len(breaches),
        },
        "checks": checks,
        "breaches": breaches,
        "webhook": {
            "configured": bool(webhook_url),
            "delivered": False,
            "result": None,
        },
    }

    webhook_failed = False
    if webhook_url:
        delivered, result = _post_webhook(
            webhook_url,
            {
                "timestamp": report["timestamp"],
                "status": report["status"],
                "passed": report["passed"],
                "summary": report["summary"],
                "breaches": report["breaches"],
                "metrics_json": report["metrics_json"],
            },
            timeout_seconds=float(args.webhook_timeout_seconds),
        )
        report["webhook"]["delivered"] = delivered
        report["webhook"]["result"] = result
        webhook_failed = not delivered

    _write_json(out_path, report)
    print(
        " ".join(
            [
                f"status={report['status']}",
                f"checks={report['summary']['checks']}",
                f"breaches={report['summary']['breaches']}",
                f"out={out_path}",
            ]
        )
    )

    if not passed:
        return 2
    if webhook_failed and bool(args.fail_on_webhook_error):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
