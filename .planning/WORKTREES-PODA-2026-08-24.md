# Inventario de los 28 worktrees — lista de poda con evidencia

> Qué cubre: los 28 worktrees de memorymaster, con commits únicos frente a `main` y archivos
> sucios, para decidir cuáles podar. **No podé ninguno**: la poda es del operador.
> Términos clave: worktree, commits únicos, `rev-list main..`, archivos sucios.
> Leer cuando: se vaya a limpiar el árbol o se investigue por qué el checkout canónico
> se desactualiza solo.

**Medido:** 2026-08-24, desde `Py Apps/memorymaster`.
**Criterio de "únicos":** `git rev-list --count main..<sha>` — trabajo NO alcanzable desde
`main`. Cero únicos = todo su contenido ya está en main.
**Criterio de "sucios":** `git status --porcelain` dentro de cada worktree.

---

## Por qué importa

Estos 28 son la causa raíz de que tu carpeta de trabajo se desactualice sola: el trabajo se
hace en los worktrees laterales, cada uno sube su rama, el PR se mergea en GitHub, y el
checkout canónico nunca baja el resultado. Nada te avisa.

Hoy además mostraron un segundo costo: al repuntar el canónico a `main` desaparecieron 111
archivos que vivían sin commitear, y uno de ellos era el script del digest semanal, que una
tarea programada invoca por ruta. Eso rompió `MemoryMasterWeeklyDigest` con exit 2.

## PODABLES sin riesgo — 10

Cero commits únicos **y** cero archivos sucios. Todo su contenido está en `main`.

| carpeta | rama |
|---|---|
| `mmchk` | (detached) |
| `mmpre` | (detached) |
| `mmpre2` | `backup/main-pre-merge-20260823` |
| `memorymaster-autoresearch-20260803` | `experiment/autoresearch-retrieval-20260803` |
| `memorymaster-compiled-profile-20260812` | `feat/compiled-user-profile` |
| `memorymaster-deploy-20260813` | (detached) |
| `memorymaster-hermes-scope-skills-20260807` | `feat/hermes-scope-skills` |
| `memorymaster-p5-graph-repair-deploy-20260809` | (detached) |
| `memorymaster-ppr7-graph-observations-20260812` | `feat/ppr7-graph-observations` |
| `memorymaster-upstream-audit-20260803` | `research/upstream-delta-20260803` |

**Advertencia sobre `mmchk`, `mmpre` y `mmpre2`:** son míos, de las mediciones de ayer y hoy.
Esos tres los puedo borrar yo cuando digas.

**Advertencia sobre `memorymaster-hermes-scope-skills-20260807`:** la tarea programada
`MemoryMaster-Hermes-24h-Check` **apunta a este worktree por ruta absoluta**. Podarlo sin
tocar la tarea la deja apuntando al vacío. Esa tarea ya está en exit 21 y su última corrida
fue el 12 de agosto, así que probablemente convenga retirarla junto con el worktree — pero
es decisión tuya.

## CON trabajo único — no tocar — 18

Ordenados por cuánto se perdería.

| únicos | rama | carpeta |
|---:|---|---|
| 10 | `feat/longmemeval-harness` | `_mm-lme` |
| 8 | `feat/probe-scoreboard` | `_mm-score` |
| 6 | `fix/recall-reaches-ancestor-scopes` | `_mm-ops` |
| 4 | `fix/v47-semantic-acceptance` | `memorymaster-v47-acceptance-20260813` |
| 4 | `fix/mm-rulings-spend-and-freeze-20260824` | **`memorymaster` — el canónico, es donde trabajás hoy** |
| 3 | `audit/T-0121-integrity` | `memorymaster-integrity-audit-20260813` |
| 3 | `fix/intake-private-context` | `memorymaster-t0127-intake-20260813` |
| 3 | `worktree-agent-a74a6cfd48d80093d` | `agent-a74a6cfd48d80093d` |
| 2 | `docs/credits-refresh` | `_mm-credits` |
| 2 | `fix/entity-graph-explicit-sensitivity-2026081…` | `memorymaster-readme-release-20260813` |
| 1 | `release/v4.8.3` | `_mm-es` |
| 1 | `fix/perf-test-measures-work` | `_mm-perf` |
| 1 | `release/v4.8.4` | `_mm-sup` |
| 1 | `fix/observation-confidence-staleness-2026081…` | `memorymaster-observation-staleness-20260815` |
| 1 | `release/v4.7.1` | `memorymaster-v471-release-20260813` |
| 1 | `chore/test-reliability` | `agent-a420073109589a7b3` |
| 1 | `evolve/entity-communities` | `wf_a52644d0-cb4-1` |
| 1 | `evolve/local-rerank` | `wf_a52644d0-cb4-2` |

Los de 1 commit único son candidatos a revisar: puede ser un commit que valga la pena
mergear, o uno ya superado. Ninguno se puede podar sin mirar qué dice ese commit.

## Lo que NO hice

No podé ninguno, no borré ninguna rama, no toqué ningún archivo de esos worktrees.

Y una salvedad de método: **"0 commits únicos" es condición necesaria pero no suficiente**.
Un worktree sin commits únicos puede tener archivos sin commitear — por eso la tabla mide
las dos cosas. Hoy los 10 podables tienen ambos en cero, pero volver a medir antes de borrar
cuesta un comando y evita exactamente lo que pasó con el digest semanal.
