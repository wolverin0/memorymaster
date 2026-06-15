from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from pathlib import Path
from typing import Any

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _DaydreamInsight:
    title: str
    connection: str
    synthesis: str
    implication: str
    average: float | None
    date: str
    references: list[dict[str, str]]
    path: Path


def ingest_insights(
    service: MemoryService,
    insights_dir: Path,
    *,
    min_score: float = 7.0,
    scope: str = "user",
    dry_run: bool = False,
) -> dict:
    """Ingest accepted daydream insights as candidate hypothesis claims.

    Honours per-cycle LLM budget caps from ``llm_budget``. Today this path
    does not itself call ``call_llm`` (insights arrive pre-scored), but the
    wrapper is in place so downstream changes (re-scoring, paraphrase
    detection, etc.) inherit the same abort semantics as ``service.run_cycle``
    and ``wiki_engine.absorb``. If a parent scope is already open, defers
    to it instead of opening a nested one.
    """
    from memorymaster.govern import llm_budget

    if llm_budget.get_current() is not None:
        return _ingest_insights_impl(
            service, insights_dir, min_score=min_score, scope=scope, dry_run=dry_run
        )

    with llm_budget.cycle_scope() as budget:
        try:
            result = _ingest_insights_impl(
                service, insights_dir, min_score=min_score, scope=scope, dry_run=dry_run
            )
        except llm_budget.LLMBudgetExceeded as exc:
            result = {
                "ingested": 0,
                "skipped": 0,
                "errors": [
                    f"daydream ingest aborted by llm budget: reason={exc.reason}"
                ],
                "aborted": True,
                "aborted_reason": exc.reason,
                "aborted_provider": exc.provider,
            }
        result["budget"] = budget.snapshot()
        return result


def _ingest_insights_impl(
    service: MemoryService,
    insights_dir: Path,
    *,
    min_score: float = 7.0,
    scope: str = "user",
    dry_run: bool = False,
) -> dict:
    """Original ingest_insights implementation, called inside a budget scope."""
    root = Path(insights_dir)
    result: dict[str, Any] = {"ingested": 0, "skipped": 0, "errors": []}
    if not root.exists() or not root.is_dir():
        raise ValueError(f"insights_dir does not exist or is not a directory: {root}")

    for path in _iter_insight_files(root):
        insight = _load_insight(path)
        if insight is None:
            result["skipped"] += 1
            result["errors"].append(f"{path}: could not parse daydream insight")
            continue

        if insight.average is None or insight.average < min_score:
            result["skipped"] += 1
            continue

        idempotency_key = _idempotency_key(insight)
        if _claim_exists(service, idempotency_key):
            result["skipped"] += 1
            continue

        if dry_run:
            result["ingested"] += 1
            continue

        claim = service.ingest(
            text=insight.synthesis,
            citations=_citations_for(insight),
            idempotency_key=idempotency_key,
            claim_type="hypothesis",
            subject=insight.title or _subject_from_connection(insight.connection),
            scope=scope,
            volatility="medium",
            confidence=0.5,
            source_agent="daydream",
        )
        if claim.idempotency_key == idempotency_key:
            result["ingested"] += 1
        else:
            result["skipped"] += 1

    return result


def _iter_insight_files(root: Path) -> list[Path]:
    files = [path for path in root.rglob("*.json") if path.is_file()]
    files.extend(
        path
        for path in root.rglob("*.md")
        if path.is_file() and "digests" not in {part.lower() for part in path.parts}
    )
    return sorted(files)


def _load_insight(path: Path) -> _DaydreamInsight | None:
    try:
        if path.suffix.lower() == ".json":
            return _load_json_insight(path)
        if path.suffix.lower() == ".md":
            return _load_markdown_insight(path)
    except Exception as exc:
        logger.warning("Skipping daydream insight %s: %s", path, exc)
    return None


def _load_json_insight(path: Path) -> _DaydreamInsight | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        logger.warning("Skipping daydream manifest-like JSON %s", path)
        return None
    if not isinstance(data, dict):
        return None

    metadata = _dict_value(data.get("metadata"))
    scores = _dict_value(metadata.get("scores"))
    average = _float_value(
        data.get("avg_score")
        or data.get("average")
        or scores.get("average")
        or scores.get("avg_score")
    )
    title = _text_value(data.get("title") or data.get("suggested_title"))
    connection = _text_value(data.get("connection"))
    synthesis = _text_value(data.get("synthesis"))
    implication = _text_value(data.get("implication"))
    if not synthesis:
        return None

    references = _references_from_json(data, metadata)
    date = _normalize_date(
        data.get("date")
        or data.get("created_date")
        or metadata.get("date")
        or metadata.get("created_date")
        or _date_from_filename(path)
    )
    return _DaydreamInsight(
        title=title or _subject_from_connection(connection),
        connection=connection,
        synthesis=synthesis,
        implication=implication,
        average=average,
        date=date,
        references=references,
        path=path,
    )


def _load_markdown_insight(path: Path) -> _DaydreamInsight | None:
    raw = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(raw)
    if frontmatter.get("type") != "daydream":
        return None
    title = _first_heading(body) or path.stem
    average = _float_value(frontmatter.get("scores.average"))
    if average is None:
        average = _float_value(frontmatter.get("average"))
    connection = _blockquote_connection(body)
    synthesis = _section_before(body, "## Implication").strip()
    if title:
        synthesis = re.sub(rf"^\s*#\s+{re.escape(title)}\s*", "", synthesis).strip()
    synthesis = re.sub(r"^>\s*Connection.*$", "", synthesis, flags=re.MULTILINE).strip()
    implication = _section_between(body, "## Implication", "## Critic")
    if not synthesis:
        return None
    date = _normalize_date(frontmatter.get("created_date") or _date_from_filename(path))
    return _DaydreamInsight(
        title=title,
        connection=connection,
        synthesis=synthesis,
        implication=implication,
        average=average,
        date=date,
        references=_references_from_frontmatter(frontmatter),
        path=path,
    )


def _references_from_json(data: dict[str, Any], metadata: dict[str, Any]) -> list[dict[str, str]]:
    raw_refs = metadata.get("references") or data.get("references") or []
    references: list[dict[str, str]] = []
    if isinstance(raw_refs, list):
        for item in raw_refs:
            if isinstance(item, dict):
                locator = _text_value(item.get("path") or item.get("locator") or item.get("file"))
                title = _text_value(item.get("title") or item.get("name"))
            else:
                locator = _text_value(item)
                title = ""
            if locator:
                references.append({"path": locator, "title": title})
    for side in ("a", "b"):
        locator = _text_value(data.get(f"path_{side}"))
        title = _text_value(data.get(f"title_{side}"))
        if locator and not any(ref["path"] == locator for ref in references):
            references.append({"path": locator, "title": title})
    return references


def _references_from_frontmatter(frontmatter: dict[str, str]) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    for value in frontmatter.get("source_notes", "").splitlines():
        cleaned = _clean_wikilink(value.strip().lstrip("-").strip().strip("'\""))
        if cleaned:
            references.append({"path": cleaned, "title": cleaned})
    return references


def _citations_for(insight: _DaydreamInsight) -> list[CitationInput]:
    citations = [
        CitationInput(source="daydream", locator=ref["path"], excerpt=ref.get("title") or None)
        for ref in insight.references
        if ref.get("path")
    ]
    if citations:
        return citations
    return [
        CitationInput(
            source="daydream",
            locator=str(insight.path),
            excerpt=insight.connection or insight.title,
        )
    ]


def _claim_exists(service: MemoryService, idempotency_key: str) -> bool:
    getter = getattr(service.store, "get_claim_by_idempotency_key", None)
    return bool(getter and getter(idempotency_key, include_citations=False))


def _idempotency_key(insight: _DaydreamInsight) -> str:
    title = re.sub(r"\s+", " ", insight.title.strip().lower())
    return f"daydream:{insight.date}:{title}"


def _subject_from_connection(connection: str) -> str:
    words = re.findall(r"[A-Za-z0-9_]+", connection)
    return " ".join(words[:3]) if words else "daydream insight"


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text_value(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _float_value(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_date(value: Any) -> str:
    text = _clean_wikilink(_text_value(value))
    match = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return "unknown-date"


def _date_from_filename(path: Path) -> str:
    return _normalize_date(path.stem)


def _clean_wikilink(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(r"\[\[([^|\]]+)(?:\|[^\]]+)?\]\]", value)
    return match.group(1).strip() if match else value


def _split_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    return _parse_frontmatter(parts[1]), parts[2]


def _parse_frontmatter(raw: str) -> dict[str, str]:
    data: dict[str, str] = {}
    current_list: str | None = None
    current_map: str | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.startswith("  -") and current_list:
            existing = data.get(current_list, "")
            data[current_list] = f"{existing}\n{line.strip()}".strip()
            continue
        if line.startswith("  ") and current_map and ":" in line:
            key, value = line.strip().split(":", 1)
            data[f"{current_map}.{key.strip()}"] = value.strip().strip("'\"")
            continue
        current_map = None
        current_list = None
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        data[key] = value
        if not value:
            current_list = key
            current_map = key
    return data


def _first_heading(body: str) -> str:
    match = re.search(r"^#\s+(.+)$", body, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _blockquote_connection(body: str) -> str:
    match = re.search(r"^>\s*(.+)$", body, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _section_before(body: str, heading: str) -> str:
    index = body.find(heading)
    return body if index < 0 else body[:index]


def _section_between(body: str, start: str, end_prefix: str) -> str:
    start_index = body.find(start)
    if start_index < 0:
        return ""
    content_start = start_index + len(start)
    end_match = re.search(rf"^{re.escape(end_prefix)}.*$", body[content_start:], flags=re.MULTILINE)
    if not end_match:
        return body[content_start:].strip()
    return body[content_start:content_start + end_match.start()].strip()
