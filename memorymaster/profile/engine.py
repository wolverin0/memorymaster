"""Resumable weekly map/reduce engine for the compiled user profile."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from memorymaster.profile.models import (
    ProfileCandidate,
    ProfileDecision,
    ProfileFact,
    ProfileMessage,
    ProfileValidationError,
)
from memorymaster.profile.renderer import render_profile
from memorymaster.profile.repository import ProfileRepository

# El lote tiene que CABER en el prompt del proveedor, y ese tope cambio.
#
# HISTORIA, porque explica los dos numeros: el default era 200.000 contra un
# tope duro de 30.000 (`agy` recibia el prompt en -p y Windows corta la linea de
# comandos), asi que cada map call moria ANTES de invocar al proveedor y el run 3
# quedo OCHO DIAS en `mapping`. El 2026-08-28 se bajo a 20.000 para desatascarlo,
# lo que funciono pero multiplico por doce la cantidad de llamadas: a ~17
# mensajes por lote el run entero pasaba a necesitar ~775 llamadas, y cada
# llamada a `agy` paga ~20.000 tokens fijos de andamiaje.
#
# Con el transporte por STDIN (stream-json) el tope subio a 400.000, asi que el
# lote vuelve a 200.000: ~65 llamadas en vez de ~775.
# tests/test_profile_batch_fits_provider.py pina el ACOPLE contra el cliente, no
# el numero, y por eso sigue verde con los dos valores.
DEFAULT_MAX_INPUT_CHARS = 200_000

logger = logging.getLogger(__name__)


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
    max_messages: int = 500
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS
    min_independent_sessions: int = 2
    preference_ttl_days: int = 90
    # 2026-08-30: con el run 3 destrabado el corpus paso a 52 hechos y el techo
    # viejo (800 tok / 40 hechos) cortaba 20 en silencio — la proyeccion quedaba
    # clavada en 800/800 justo cuando los hechos NUEVOS eran los buenos
    # (memorymaster+UNMS sup=10, WISP, Task Scheduler) y los viejos los stale.
    # Truncar por techo no avisa, asi que el techo sube con el corpus.
    token_budget: int = 1400
    max_facts: int = 60
    # 68 candidatos particionaron bien (run 2); 234 no lo lograron ni una vez en
    # diez dias (run 3). 40 deja margen bajo el limite observado sin volver el
    # reduce innecesariamente charlatan.
    reduce_batch_size: int = 40

    @classmethod
    def from_env(cls) -> "ProfileConfig":
        defaults = cls()
        return cls(
            cadence_days=_env_int("MEMORYMASTER_PROFILE_CADENCE_DAYS", 7),
            max_map_calls=_env_int("MEMORYMASTER_PROFILE_MAX_MAP_CALLS", 3),
            max_messages=_env_int("MEMORYMASTER_PROFILE_MAX_MESSAGES", 500),
            max_input_chars=_env_int(
                "MEMORYMASTER_PROFILE_MAX_INPUT_CHARS", DEFAULT_MAX_INPUT_CHARS
            ),
            min_independent_sessions=_env_int("MEMORYMASTER_PROFILE_MIN_SESSIONS", 2),
            preference_ttl_days=_env_int("MEMORYMASTER_PROFILE_PREFERENCE_TTL_DAYS", 90),
            token_budget=_env_int("MEMORYMASTER_PROFILE_TOKEN_BUDGET", defaults.token_budget),
            max_facts=_env_int("MEMORYMASTER_PROFILE_MAX_FACTS", defaults.max_facts),
            reduce_batch_size=_env_int("MEMORYMASTER_PROFILE_REDUCE_BATCH", 40),
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
            # El NOMBRE de la clase no alcanza como diagnostico. Medido el
            # 2026-08-29: el run 3 devolvia 'ProfileValidationError' y nada mas,
            # y esa clase tiene siete causas distintas (categoria desconocida,
            # predicado no coincidente, valor con forma de instruccion, material
            # sensible...). Sin el mensaje hay que bisecar a mano cual de las
            # siete fue, que es el mismo costo que ya pago AntigravityError.
            detail = str(exc).strip()
            logger.warning(
                "compiled profile run %s fallo en %s: %s: %s",
                run["id"], run["status"], type(exc).__name__, detail[:500],
            )
            self.repo.record_error(int(run["id"]), type(exc).__name__, now=current)
            return {
                "ok": False,
                "status": str(run["status"]),
                "error": type(exc).__name__,
                "detail": detail[:500],
            }

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
        """Reduce por LOTES acotados, no de una.

        El validador exige que el modelo particione el lote perfectamente: cada
        candidate_id exactamente una vez. Eso se sostiene con decenas de
        candidatos y no con cientos — el run 2 completo con 68, el run 3 acumulo
        234 y quedo clavado diez dias, fallando entre "candidates must appear
        exactly once" y JSON malformado.

        La semantica se conserva releyendo `active_facts()` entre lotes: el lote
        N+1 ve los hechos que creo el N y puede fusionar contra ellos, que es el
        mismo mecanismo incremental que ya opera entre runs. Lo que cambia es que
        las fusiones se deciden con visibilidad parcial, asi que dos candidatos
        de lotes distintos pueden quedar como dos hechos donde una particion
        unica los unia. Ese es el precio, y es preferible a no reducir nunca.
        """
        stats = {"applied": 0, "rejected": 0, "consumed": 0}
        batches = 0
        while True:
            pending = self.repo.candidates(run_id, pending_only=True)
            if not pending:
                break
            batch = pending[: self.config.reduce_batch_size]
            facts = self.repo.active_facts()   # releido: el lote previo creo hechos
            decisions = self.reducer.reduce(batch, facts)
            applied = self.repo.apply_decisions(
                run_id,
                decisions,
                now=now,
                min_sessions=self.config.min_independent_sessions,
            )
            for key, value in applied.items():
                stats[key] = stats.get(key, 0) + value
            batches += 1
            if applied.get("consumed", 0) == 0:
                # Ningun candidato quedo marcado: reintentar seria un bucle
                # infinito sobre el mismo lote. Se corta y el run queda en
                # `reducing` para el proximo ciclo, que es el estado honesto.
                raise ProfileValidationError(
                    f"lote de {len(batch)} candidatos no consumio ninguno"
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
    from memorymaster.profile.providers import ProfileMapper, ProfileReducer


    # MEMORYMASTER_PROFILE_OUTPUT_DIR existe para que los tests puedan sacar esta
    # escritura del HOME real, igual que MEMORYMASTER_SNAPSHOT_DIR y
    # MEMORYMASTER_SPOOL_DIR. Sin ella no habia forma: `scheduled_task._run_dream`
    # llama a esta funcion SIN output_dir, asi que cualquier test que ejercite ese
    # camino con una base temporal vacia sobrescribia el perfil compilado del
    # operador —el que se inyecta en cada sesion— dejandolo en cero hechos.
    # Verificado el 2026-08-24: correr tests/test_scheduled_task_runtime.py
    # cambiaba el mtime de ~/.memorymaster/projections/user.md.
    configured = os.environ.get("MEMORYMASTER_PROFILE_OUTPUT_DIR", "").strip()
    directory = (
        output_dir
        or (Path(configured) if configured else Path.home() / ".memorymaster" / "projections")
    )
    engine = CompiledProfileEngine(
        ProfileRepository(db_path),
        ProfileMapper(),
        ProfileReducer(),
        output_dir=directory,
    )
    return engine.run(force=force, max_map_calls=max_map_calls)


__all__ = ["CompiledProfileEngine", "ProfileConfig", "run_compiled_profile"]
