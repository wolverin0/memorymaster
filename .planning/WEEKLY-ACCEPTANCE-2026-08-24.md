# Revisión semanal de aceptación — 2026-08-24

> Qué cubre: medición de PPR-7 (7 días), perfil compilado, tareas programadas y
> respaldos, sobre la base viva en sólo-lectura. Veredicto global **NO GREEN**, con dos
> hallazgos de seguridad/operación que bloquean. Términos clave: llm-stop-hook, ingesta
> sin filtro, backups inexistentes, discovery_unrecorded, watermark trabado.
> Leer cuando: se retome la aceptación semanal o se investigue ingesta sin sanitizar.

**Base:** `memorymaster.db`, 7.190,9 MB, leída con `mode=ro`; `database_mutations: 0`.
**Rama:** `fix/mm-rulings-spend-and-freeze-20260824` @ `0e1739e`. Sin push.
**Ventana:** 168 horas.

---

## Veredicto: NO GREEN

`operational_review --lookback-hours 168` devuelve `verdict: FAIL`, **exit code 1**.

> Corrección de método, para que no se repita: mi primera lectura del exit fue `0`, pero
> era el de `tail` al final del pipe, no el del comando. El real es 1. Un exit code leído
> a través de una tubería no es el exit code del programa.

**No llamo verde a esto por registro de tareas ni por exit codes**, y hay dos razones
concretas por las que eso habría mentido hoy: `Checkpoint-Daily` devuelve 0 sin hacer
backups, y el check `graph_observations` devuelve PASS con 6.752 outcomes sin registrar.

---

## BLOQUEANTE 1 — ingesta que evita el filtro de sensibilidad

`recent_private_context`: **FAIL**, 4 coincidencias sobre 3.685 claims en 7 días.

| id | patrón | scope | status | agente | creada |
|---|---|---|---|---|---|
| 132084 (`mm-1bd4`) | `private_ipv4` | `project:yolo26` | confirmed | `llm-stop-hook` | 19-08 |
| 134056 | `absolute_path_windows` | `project:wezbridge` | conflicted | `llm-stop-hook` | 23-08 |
| 134091 | `absolute_path_windows` | `project:wezbridge` | conflicted | `llm-stop-hook` | 23-08 |
| 134099 | `absolute_path_windows` | `project:wezbridge` | confirmed | `llm-stop-hook` | 23-08 |

Contenido deliberadamente omitido (regla `sensitivity-filter.md`).

**El filtro no falló: no se ejecutó.** Corriendo hoy `sanitize_claim_input` —la que usan
tanto `svc.ingest` como el spool— contra el texto guardado, marca los cuatro y los
redactaría. Están crudos en la base.

**Causa raíz: el hook instalado no es el del repo.**

```
~/.claude/hooks/memorymaster-auto-ingest.py     4 de mayo
  linea 144:  sqlite3.connect(DB_PATH)
  linea 155:  INSERT INTO claims (...)
```

Escribe **SQL crudo**: sin servicio, sin spool, sin sanitizador. La plantilla del repo
(`memorymaster/config_templates/hooks/`) ya enruta por `MemoryService.ingest` o por el
spool, y ambos filtran. La copia instalada quedó de una versión anterior.

Es la regla de `sensitivity-filter.md` incumplida al pie de la letra: *"Any new ingest
path — default-deny until filter is wired in"*.

**Detalle incómodo:** la línea 219 de ese mismo hook instalado emite el recordatorio
*"NEVER ingest credentials, IPs, tokens, or code"*. El hook que pide el cuidado es el que
evita el mecanismo que lo haría cumplir. Prosa donde hacía falta un chequeo.

**No lo reparé:** `~/.claude/` es del operador. Remedio propuesto: reinstalar el hook
desde la plantilla (`python scripts/setup-hooks.py`), y después decidir sobre los 4 claims
—`redact_claim_payload` es lo prescrito, pero dos están `confirmed` y participan del
recall, así que la decisión es tuya.

## BLOQUEANTE 2 — no existe respaldo restaurable

| dónde | qué hay |
|---|---|
| `backups/` | **0** archivos `.db` completos; 4 sidecars `-shm`/`-wal` del 15–16 ago; un `.db.partial` sin su `.db` |
| `snapshots/` | sólo restos de `perf-smoke-*`, `s`, `smoke` (0,33–0,62 MB) y directorios vacíos |
| mayor `.db` bajo `~/.memorymaster` | `capture-control.db`, 26,9 MB |
| base viva | 7.190,9 MB |

**Último backup restaurable: ninguno.**

`MemoryMaster-Checkpoint-Daily` corrió hoy 11:25 y devolvió **0**. Su acción es
`memorymaster-poke.cmd daily-feature-review.txt` — un disparador de revisión de features.
El nombre promete un checkpoint; la acción hace otra cosa; el exit code es honesto sobre
lo que ejecuta y engañoso sobre lo que su nombre sugiere.

**Espacio de nombres de snapshots contaminado** por corridas de test (`perf-smoke-*`,
`s`, `smoke`). El fixture `_hermetic_snapshot_dir` ya frena las nuevas; los restos viejos
siguen ahí. No los borré — podar es del operador.

---

## PPR-7 — siete días

| métrica | valor |
|---|---|
| jobs `discover` en 7d | **4.039**, todos `completed` |
| jobs `synthesize` en 7d | **0** |
| observaciones emitidas en 7d | **0** (las 2 existentes son del 14-08) |
| pendientes / leased / retryable / bloqueados | 0 / 0 / 0 / 0 |
| leases vencidos | 0 |
| reintentos | máx. `attempts=1` — ningún job reintentó |
| latencia `created→completed` | 67,7 s de media |
| filas de soporte de aristas | 348 |
| **`unknown_sensitivity_rows`** | **0** |

**Precisión de observaciones a 7 días: no medible — no hubo observaciones.** Decirlo así
y no "100%" es la diferencia entre medir y suponer.

**Hipótesis previa descartada.** Un claim anterior atribuía el vacío a 306 supports
excluidos por sensibilidad desconocida. Hoy `unknown_sensitivity_rows = 0`: esa ya no es
la causa.

**Lo que sí bloquea el diagnóstico:**

```
completed_discovery:          6752
discovery_unrecorded:         6752
discovery_outcomes_recorded:      0
```

El motor distingue `discovery_no_supports` de `discovery_no_components` — exactamente la
respuesta a *"¿por qué no sintetiza?"*. La migración **0022** agregó la columna `outcome`
para persistirlo. **Nada la escribe**: 6.754 filas con `outcome` y `error_code` en NULL.

Y el check devuelve **PASS** contando 6.752 outcomes sin registrar. Mide la ceguera y no
la reporta.

**Corrección de soporte/citas:** `graph_observation_supports` = 40 filas para 2
observaciones, todas con `support_hash`, `algorithm_version` y `ontology_version`
poblados. El gate declarado —*"every graph support requires explicit source and evidence
sensitivity"*— se cumple con 0 filas de sensibilidad desconocida.

**Recall opt-in:** `graph_observation_recall.py` es "bounded packing for **opt-in**
observations"; gate `MEMORYMASTER_GRAPH_OBSERVATIONS` (=1). Impacto real esta semana:
**cero**, porque no hay observaciones nuevas que recuperar.

**Tope de 3 síntesis por scope/hora:** `MAX_SYNTHESIS_CALLS_PER_SCOPE = 3`, usado como
`limit` de `lease_jobs`; la clave de dedup incluye `cycle_hour`. Cableado, pero **no
ejercido** — no hubo llamadas que topear.

---

## Perfil compilado

| métrica | valor | veredicto |
|---|---|---|
| hechos activos | 29 | ok |
| filas de soporte exactas | 95 | ok |
| desajustes soporte↔hecho | 0 | ok |
| sesiones independientes | mín. 2 · media 2,2 · máx. 4 | **regla de ≥2 se cumple: 0 incumplen** |
| `support_count` | mín. 2 · media 3,3 · máx. 9 | ok |
| cotas | 788 tokens / 29 hechos (cotas 800 / 40) | ok |
| entrega en SessionStart | verificada de primera mano | ok |
| marca de generado | presente | ok |
| manifiesto | `memorymaster.compiled-profile.v1`, 29 facts con IDs | ok |
| modelos | map `glm-5-turbo` · reduce `glm-5.2` | coinciden |

**Frescura: el soporte más nuevo es del 9 de agosto — 15 días.** Nada refrescó desde
entonces.

**Watermark TRABADO.** Run 3 en estado `mapping` desde el 20-08 con
`error_code=OpenCodeClientError` y `map_calls=0`. Watermark clavado en 10.171.802 contra
objetivo 10.184.941. Al deshabilitar Dreaming (ruling MM4) corté además el proceso que lo
hacía avanzar; ya venía fallando, pero conviene decirlo.

**Expiración de preferencias: nunca ocurrió.** Los 29 hechos están `active`; no hay una
sola fila `expired` ni retirada. Con soporte de hasta 18 días y TTL configurado, cero
expiraciones es un dato a explicar, no a celebrar.

**Salida insegura rechazada: NO MEDIBLE.** `compiled_profile_candidates` no tiene columna
de rechazo, y `compiled_profile_runs` tampoco guarda el conteo. `run()` devuelve
`stats["rejected"]` en memoria y se pierde con el proceso. 108 candidatos → 29 hechos: 79
no promovidos, sin registro de por qué. **Una revisión semanal que debe medir esto no
puede hacerlo con la instrumentación actual.**

---

## Tareas programadas y proveedores

| tarea | estado | último exit |
|---|---|---|
| `MemoryMaster-Dreaming` | **Disabled** (por ruling MM4) | 1 |
| `MemoryMasterSteward` | Ready | 0 |
| `MemoryMaster-HermesSync-AM` / `-PM` | Ready | 0 / 0 |
| **`MemoryMaster-Hermes-24h-Check`** | Ready | **21** |
| `MemoryMaster-MCP-HTTP-Hermes` | **Running** | 267009 = `STILL_RUNNING`, normal |
| `MemoryMaster-Checkpoint-Daily` / `-Weekly` | Ready | 0 / 0 — *pero ver bloqueante 2* |
| **`MemoryMasterWeeklyDigest`** | Ready | **2** |
| `MemoryMaster-Operational-Review` | Ready | 0 |

Dos fallando sin causa investigada: **Hermes-24h-Check (21)** y **WeeklyDigest (2)**.

**Costo por proveedor (ledger de Dreaming, acumulado):**

| proveedor | llamadas | yield | tokens entrada | 429 |
|---|---:|---:|---:|---:|
| google | 540 | 0,489 | 3.889.063 | 98 |
| **openai** | 527 | **0,896** | **9.762.549** | 0 |
| zai-coding-plan | 216 | 0,394 | 3.780.048 | 0 |

`openai` no está en el spec Gemini/GLM. Inactivo en las últimas 24 h, pero alcanzaba una
variable de entorno para volver. Ya tiene tope en tokens (ruling MM3, commit `9b3b58f`).

---

## Reparado esta semana, con evidencia

| qué | commit | evidencia |
|---|---|---|
| La suite vaciaba el perfil del operador | `0e1739e` | mtime cambiaba; ahora hash idéntico tras 4.732 tests; mutación 4/5 |
| Perfil restaurado | — | 0 → 29 hechos, 788 tokens, manifiesto con 95 soportes |
| Escrituras de Qdrant congeladas (MM8) | `9b3b58f` | 18 tests; mutación 9/18; lecturas intactas |
| Tope de gasto en tokens (MM3) | `9b3b58f` | 12 tests; el tope viejo contaba llamadas |
| CLI de `dream-run` ignoraba el proveedor configurado | `9b3b58f` | test anclado en el requisito |
| Dreaming deshabilitado (MM4) | — | tarea en `Disabled` |
| `release-truth` regenerado | `0e1739e` | 3986 → 4011 funciones de test |

**Suite completa: 4.732 passed, 1 failed → reparado → 11 passed.** `ruff` limpio.

## Bloqueado, gateado al operador

1. **Backups.** No hay ninguno y ninguna tarea los produce.
2. **Hook instalado que evita el filtro.** `~/.claude/` es tuyo.
3. **4 claims con contexto privado** — redacción pendiente de tu decisión.
4. **Watermark del perfil trabado** desde el 20-08.
5. **`Hermes-24h-Check` (21)** y **`WeeklyDigest` (2)**.
6. **PPR-7 sin diagnóstico**: la columna `outcome` de la migración 0022 no la escribe nadie.
7. **MM7** (`verbatim-cleanup --analyze-only`): sigue sin emitir una línea tras >40 min
   sobre la tabla de 1,08 GB. La primera corrida la maté yo con un `timeout` — **eso no
   es un fallo del comando** y no debe citarse como tal.
