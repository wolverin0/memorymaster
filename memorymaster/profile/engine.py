"""Resumable weekly map/reduce engine for the compiled user profile."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from memorymaster.profile.models import ProfileCandidate, ProfileDecision, ProfileFact, ProfileMessage
from memorymaster.profile.renderer import render_profile
from memorymaster.profile.repository import ProfileRepository


class ProfileMapper(Protocol):
    model: str

    def map(self, messages: tuple[ProfileMessage, ...]) -> tuple[ProfileCandidate, ...]: ...


class ProfileReducer(Protocol):
    model: str

    def reduce(
        self, candidates: tuple[ProfileCandidate, ...], facts: tuple[ProfileFact, ...]
    ) -> tuple[ProfileDecision, ...]: ...


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    cadence_days: int = 7
    max_map_calls: int = 3
    max_messages: int = 1000
    max_input_chars: int = 500_000
    min_independent_sessions: int = 2
    preference_ttl_days: int = 90
    token_budget: int = 800
    max_facts: int = 40

    @classmethod
    def from_env(cls) -> "ProfileConfig":
        return cls(
            cadence_days=_env_int("MEMORYMASTER_PROFILE_CADENCE_DAYS", 7),
            max_map_calls=_env_int("MEMORYMASTER_PROFILE_MAX_MAP_CALLS", 3),
            max_messages=_env_int("MEMORYMASTER_PROFILE_MAX_MESSAGES", 1000),
            max_input_chars=_env_int("MEMORYMASTER_PROFILE_MAX_INPUT_CHARS", 500_000),
            min_independent_sessions=_env_int("MEMORYMASTER_PROFILE_MIN_SESSIONS", 2),
            preference_ttl_days=_env_int("MEMORYMASTER_PROFILE_PREFERENCE_TTL_DAYS", 90),
            token_budget=_env_int("MEMORYMASTER_PROFILE_TOKEN_BUDGET", 800),
            max_facts=_env_int("MEMORYMASTER_PROFILE_MAX_FACTS", 40),
        )


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


class CompiledProfileEngine:
    def __init__(
        self,
        repository: ProfileRepository,
        mapper: ProfileMapper,
        reducer: ProfileReducer,
        *,
        output_dir: str | Path,
        config: ProfileConfig | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repository
        self.mapper = mapper
        self.reducer = reducer
        self.output_dir = Path(output_dir)
        self.config = config or ProfileConfig.from_env()
        self.now = now or (lambda: datetime.now(timezone.utc))

    def run(self, *, force: bool = False, max_map_calls: int | None = None) -> dict[str, Any]:
        current = self.now()
        active = self.repo.active_run()
        if active is None and not force and not self.repo.due(
            now=current, cadence_days=self.config.cadence_days
        ):
            return {"ok": True, "status": "not_due"}
        run = active or self._start_run(current)
        if run is None:
            rendered = self._write_projection(current)
            return {"ok": True, "status": "no_changes", "facts": len(rendered.fact_ids)}
        try:
            result = self._advance_mapping(run, current, max_map_calls)
            if result is not None:
                return result
            return self._reduce_and_complete(int(run["id"]), current)
        except Exception as exc:
            self.repo.record_error(int(run["id"]), type(exc).__name__, now=current)
            return {"ok": False, "status": str(run["status"]), "error": type(exc).__name__}

    def _start_run(self, now: datetime) -> dict[str, Any] | None:
        target = self.repo.max_user_id()
        latest = self.repo.latest_completed_run()
        start = int(latest["target_watermark"]) if latest else 0
        if target <= start:
            return None
        return self.repo.start_run(
            target=target,
            map_model=self.mapper.model,
            reduce_model=self.reducer.model,
            now=now,
        )

    def _advance_mapping(
        self, run: dict[str, Any], now: datetime, max_map_calls: int | None
    ) -> dict[str, Any] | None:
        limit = max_map_calls if max_map_calls is not None else self.config.max_map_calls
        calls = 0
        while int(run["current_watermark"]) < int(run["target_watermark"]):
            batch = self.repo.message_batch(
                after_id=int(run["current_watermark"]),
                through_id=int(run["target_watermark"]),
                max_messages=self.config.max_messages,
                max_chars=self.config.max_input_chars,
            )
            provider_called = bool(batch.messages)
            if provider_called and calls >= max(1, limit):
                break
            candidates = self.mapper.map(batch.messages) if provider_called else ()
            calls += int(provider_called)
            self.repo.save_mapping(
                int(run["id"]), candidates, batch.scanned_through_id,
                now=now, provider_called=provider_called,
            )
            run = self.repo.run(int(run["id"]))
            if batch.scanned_through_id <= int(run["start_watermark"]):
                break
        if int(run["current_watermark"]) < int(run["target_watermark"]):
            return {"ok": True, "status": "mapping", "run_id": int(run["id"]), "map_calls": calls}
        self.repo.mark_reducing(int(run["id"]), now=now)
        return None

    def _reduce_and_complete(self, run_id: int, now: datetime) -> dict[str, Any]:
        candidates = self.repo.candidates(run_id)
        facts = self.repo.active_facts()
        decisions = self.reducer.reduce(candidates, facts) if candidates else ()
        stats = self.repo.apply_decisions(
            run_id,
            decisions,
            now=now,
            min_sessions=self.config.min_independent_sessions,
        )
        expired = self.repo.expire_preferences(
            now=now, ttl_days=self.config.preference_ttl_days
        )
        rendered = self._write_projection(now)
        output_hash = hashlib.sha256(rendered.markdown.encode("utf-8")).hexdigest()
        self.repo.complete_run(run_id, output_hash, now=now)
        return {
            "ok": True,
            "status": "completed",
            "run_id": run_id,
            "facts": len(rendered.fact_ids),
            "applied": stats["applied"],
            "rejected": stats["rejected"],
            "expired": expired,
        }

    def _write_projection(self, now: datetime):
        facts = self.repo.active_facts()
        rendered = render_profile(
            facts,
            token_budget=self.config.token_budget,
            max_facts=self.config.max_facts,
        )
        selected = {fact.fact_id: fact for fact in facts if fact.fact_id in rendered.fact_ids}
        manifest = {
            "schema": "memorymaster.compiled-profile.v1",
            "generated_at": now.isoformat(),
            "facts": [asdict(selected[fact_id]) for fact_id in rendered.fact_ids],
        }
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.output_dir / "user.md", rendered.markdown)
        self._atomic_write(
            self.output_dir / "user-profile.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        return rendered

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)


def run_compiled_profile(
    db_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    force: bool = False,
    max_map_calls: int | None = None,
) -> dict[str, Any]:
    from memorymaster.profile.providers import GLMProfileMapper, GLMProfileReducer

    directory = output_dir or Path.home() / ".memorymaster" / "projections"
    engine = CompiledProfileEngine(
        ProfileRepository(db_path),
        GLMProfileMapper(),
        GLMProfileReducer(),
        output_dir=directory,
    )
    return engine.run(force=force, max_map_calls=max_map_calls)


__all__ = ["CompiledProfileEngine", "ProfileConfig", "run_compiled_profile"]
