"""Source census and opt-in deep trajectory normalization."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .adapters import (
    ParsedTranscript,
    parse_claude,
    parse_codex,
    parse_history_metadata,
    parse_wezbridge_metadata,
    source_prefix_hash,
    stable_hash,
    transcript_metadata,
    wezbridge_statuses,
)
from .analysis import analyze_session
from .storage import WorkflowStore, utc_now


class WorkflowScanner:
    def __init__(
        self,
        store: WorkflowStore,
        *,
        claude_root: str | Path | None = None,
        codex_root: str | Path | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        self.store = store
        self.claude_root = Path(claude_root) if claude_root else Path.home() / ".claude"
        self.codex_root = Path(codex_root) if codex_root else Path.home() / ".codex"
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self._source_cache: dict[str, object] = {}
        self._sessions_by_source: dict[int, list] = {}

    def discover(self) -> list[tuple[Path, str, str]]:
        found: dict[str, tuple[Path, str, str]] = {}
        self._add_glob(found, self.claude_root, "projects/**/*.jsonl", "transcript", "claude")
        self._add_file(found, self.claude_root / "history.jsonl", "history", "claude")
        self._add_glob(found, self.claude_root, "usage-data/session-meta/*.json", "session_meta", "claude")
        self._add_glob(found, self.codex_root, "sessions/**/*.jsonl", "transcript", "codex")
        self._add_glob(found, self.codex_root, "archived_sessions/**/*.jsonl", "transcript", "codex")
        self._add_file(found, self.codex_root / "history.jsonl", "history", "codex")
        self._add_file(found, self.codex_root / "session_index.jsonl", "session_index", "codex")
        for root, provider in ((self.claude_root, "claude"), (self.codex_root, "codex")):
            self._add_glob(found, root, "**/skill*outcome*.jsonl", "skill_outcome", provider)
        if self.workspace_root:
            self._add_file(found, self.workspace_root / "_intel" / "events.jsonl", "wezbridge", "wezbridge")
            self._add_file(found, self.workspace_root / "_intel" / "a2a-results.jsonl", "wezbridge", "wezbridge")
        for path, provider in (
            (self.claude_root / "CLAUDE.md", "claude"),
            (self.claude_root / "settings.json", "claude"),
            (self.codex_root / "AGENTS.md", "codex"),
            (self.codex_root / "config.toml", "codex"),
            (self.codex_root / "hooks.json", "codex"),
        ):
            self._add_file(found, path, "policy", provider)
        if self.workspace_root:
            self._add_file(found, self.workspace_root / "AGENTS.md", "policy", "project")
            self._add_file(found, self.workspace_root / "CLAUDE.md", "policy", "project")
        return [found[key] for key in sorted(found)]

    @staticmethod
    def _add_file(
        found: dict[str, tuple[Path, str, str]], path: Path, kind: str, provider: str,
    ) -> None:
        if path.is_file():
            found[str(path.resolve()).lower()] = (path, kind, provider)

    def _add_glob(
        self, found: dict[str, tuple[Path, str, str]], root: Path,
        pattern: str, kind: str, provider: str,
    ) -> None:
        if not root.is_dir():
            return
        for path in root.glob(pattern):
            self._add_file(found, path, kind, provider)

    def scan(
        self, *, deep: str = "none", session_ids: Iterable[str] | None = None,
    ) -> dict[str, object]:
        if deep not in {"none", "human", "selected"}:
            raise ValueError("deep must be one of: none, human, selected")
        selected = {str(value) for value in (session_ids or [])}
        if deep == "selected" and not selected:
            raise ValueError("--deep selected requires at least one --session")
        run_id = "scan-" + uuid.uuid4().hex
        started = utc_now()
        errors: list[dict[str, str]] = []
        session_hashes: set[str] = set()
        deep_hashes: set[str] = set()
        sources = self.discover()
        self._load_cache()
        with self.store.batch():
            for path, kind, provider in sources:
                try:
                    parsed, session_ids_found = self._scan_one(path, kind, provider, deep, selected)
                    session_hashes.update(session_ids_found)
                    deep_hashes.update(parsed)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    errors.append({"source": provider + ":" + kind, "error_type": type(exc).__name__})
                    self._record_failed_source(path, kind, provider, exc)
            status = "complete" if not errors else "partial"
            self.store.connection.execute(
                """INSERT INTO scan_runs VALUES (?,?,?,?,?,?,?,?,?)""",
                (run_id, started, utc_now(), deep, status, len(sources), len(session_hashes),
                 len(deep_hashes), json.dumps(errors, sort_keys=True)),
            )
        return {
            "run_id": run_id, "status": status, "source_files": len(sources),
            "sessions": len(session_hashes), "deep_sessions": len(deep_hashes), "errors": errors,
        }

    def _scan_one(
        self, path: Path, kind: str, provider: str, deep: str, selected: set[str],
    ) -> tuple[set[str], set[str]]:
        stat = path.stat()
        previous = self._source_cache.get(_path_key(path))
        if previous is not None and _recently_indexed(previous["updated_at"]):
            cached = self._cached_source_result(previous, stat, None, kind, deep, selected)
            if cached is not None:
                return cached
        prefix = source_prefix_hash(path)
        cached = self._cached_source_result(previous, stat, prefix, kind, deep, selected)
        if cached is not None:
            return cached
        source_id = self.store.upsert_source(
            path=path, source_kind=kind, provider=provider, size=stat.st_size,
            mtime_ns=stat.st_mtime_ns, prefix_hash=prefix,
        )
        if kind == "transcript":
            metadata = transcript_metadata(path, provider)
            if metadata is None:
                return set(), set()
            should_deep = deep == "human" and metadata.session_kind in {"human", "mixed"}
            should_deep = should_deep or deep == "selected" and metadata.external_id in selected
            if not should_deep:
                parsed = ParsedTranscript(metadata, (), (), (), 0)
            else:
                parsed = parse_claude(path) if provider == "claude" else parse_codex(path)
            return self._store_transcript(
                path, source_id, parsed, deep, selected, kind, provider, prefix
            )
        if kind == "policy":
            self.store.connection.execute(
                """INSERT OR IGNORE INTO policy_snapshots(session_id,source_kind,source_hash,observed_at)
                   VALUES ('',?,?,?)""",
                (provider + ":" + path.name, _file_hash(path), utc_now()),
            )
            self.store._commit()
        metadata = self._metadata_sessions(path, kind, provider)
        for session in metadata:
            self.store.upsert_session(session, source_file_id=None)
        if kind == "wezbridge":
            self._store_wezbridge_status(path)
        cursor = _complete_cursor(path) if path.suffix == ".jsonl" else stat.st_size
        self.store.upsert_source(
            path=path, source_kind=kind, provider=provider, size=stat.st_size,
            mtime_ns=stat.st_mtime_ns, prefix_hash=prefix, cursor_offset=cursor,
        )
        return set(), {session.session_id for session in metadata}

    def _cached_source_result(
        self, previous, stat, prefix: str | None, kind: str, deep: str, selected: set[str],
    ) -> tuple[set[str], set[str]] | None:
        # Aggregate metadata is cheap to parse and can mention sessions whose
        # transcript remains the authoritative primary source.
        if kind in {"history", "session_index", "session_meta", "wezbridge"}:
            return None
        if previous is None or previous["status"] != "indexed":
            return None
        unchanged = (
            int(previous["size_bytes"]) == stat.st_size
            and int(previous["mtime_ns"]) == stat.st_mtime_ns
            and (prefix is None or previous["prefix_hash"] == prefix)
        )
        if not unchanged:
            return None
        rows = self._sessions_by_source.get(int(previous["id"]), [])
        if kind != "transcript" or deep == "none":
            return set(), {row["session_id"] for row in rows}
        needs_human = deep == "human" and any(
            row["session_kind"] in {"human", "mixed"} and not row["deep_parsed"] for row in rows
        )
        needs_selected = deep == "selected" and any(
            row["external_id"] in selected and not row["deep_parsed"] for row in rows
        )
        if needs_human or needs_selected:
            return None
        deep_ids = {row["session_id"] for row in rows if row["deep_parsed"]}
        return deep_ids, {row["session_id"] for row in rows}

    def _load_cache(self) -> None:
        self._source_cache = {
            _path_key(Path(row["source_path"])): row
            for row in self.store.connection.execute("SELECT * FROM source_files")
        }
        grouped: dict[int, list] = defaultdict(list)
        for row in self.store.connection.execute(
            """SELECT source_file_id,session_id,external_id,session_kind,deep_parsed
               FROM sessions WHERE source_file_id IS NOT NULL"""
        ):
            grouped[int(row["source_file_id"])].append(row)
        self._sessions_by_source = dict(grouped)

    def _store_wezbridge_status(self, path: Path) -> None:
        for corr, statuses in wezbridge_statuses(path).items():
            session_id = stable_hash(f"wezbridge:{corr}")
            row = self.store.connection.execute(
                "SELECT metadata_json FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                continue
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            prior = set(metadata.get("a2a_types") or [])
            metadata["a2a_types"] = sorted(prior | statuses)
            self.store.connection.execute(
                "UPDATE sessions SET metadata_json=?,updated_at=? WHERE session_id=?",
                (json.dumps(metadata, sort_keys=True), utc_now(), session_id),
            )
        self.store._commit()

    def _store_transcript(
        self, path: Path, source_id: int, parsed: ParsedTranscript | None,
        deep: str, selected: set[str], kind: str, provider: str, prefix_hash: str,
    ) -> tuple[set[str], set[str]]:
        if parsed is None:
            return set(), set()
        should_deep = deep == "human" and parsed.session.session_kind in {"human", "mixed"}
        should_deep = should_deep or deep == "selected" and parsed.session.external_id in selected
        session = parsed.session if should_deep else replace(parsed.session, deep_parsed=False)
        self.store.upsert_session(session, source_file_id=source_id)
        if should_deep:
            self.store.replace_details(session.session_id, parsed.turns, parsed.actions, parsed.feedback)
            analysis = analyze_session(session, list(parsed.turns), list(parsed.actions), list(parsed.feedback))
            self.store.update_analysis(session.session_id, analysis)
        stat = path.stat()
        self.store.upsert_source(
            path=path, source_kind=kind, provider=provider, size=stat.st_size,
            mtime_ns=stat.st_mtime_ns, prefix_hash=prefix_hash,
            cursor_offset=parsed.cursor_offset,
        )
        return ({session.session_id} if should_deep else set(), {session.session_id})

    @staticmethod
    def _metadata_sessions(path: Path, kind: str, provider: str):
        if kind in {"history", "session_index"}:
            return parse_history_metadata(path, provider)
        if kind == "wezbridge":
            return parse_wezbridge_metadata(path)
        if kind == "session_meta" and path.suffix == ".json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return []
            external = str(payload.get("session_id") or payload.get("id") or path.stem)
            from .adapters import stable_hash
            from .models import SessionRecord
            return [SessionRecord(stable_hash(f"claude:{external}"), external, "claude", "human")]
        return []

    def _record_failed_source(
        self, path: Path, kind: str, provider: str, exc: Exception,
    ) -> None:
        try:
            stat = path.stat()
            self.store.upsert_source(
                path=path, source_kind=kind, provider=provider, size=stat.st_size,
                mtime_ns=stat.st_mtime_ns, prefix_hash=source_prefix_hash(path),
                status="error", last_error=type(exc).__name__,
            )
        except OSError:
            return


def _complete_cursor(path: Path) -> int:
    offset = 0
    with path.open("rb") as handle:
        for raw in handle:
            if not raw.endswith(b"\n"):
                break
            offset += len(raw)
    return offset


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _recently_indexed(value: str, *, hours: int = 24) -> bool:
    try:
        observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - observed.astimezone(timezone.utc)
    return 0 <= age.total_seconds() < hours * 3600


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


__all__ = ["WorkflowScanner"]
