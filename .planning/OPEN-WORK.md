# OPEN-WORK — lista viva de trabajo pendiente de MemoryMaster

**Qué es:** el worklist único y con estado de lo que quedó abierto, empezando por el
día 2026-08-19. Cubre regresiones propias en `main`, huecos estructurales de CI, los
ítems del assessment de OpenViking, las metas abiertas del marcador y los gates del
operador. **Cuándo leerlo:** antes de empezar cualquier trabajo en este repo, y antes
de preguntar "¿qué falta?". **Términos:** señal inerte, floor gate, cohorte congelada,
contra-métrica, evaluator-edit, tests `ml`, LongMemEval, `zero_result_rate`.
**Regla:** un ítem se marca hecho sólo con evidencia (commit, PR, o corrida citada).

> Este archivo existe porque hasta el 2026-08-19 no había ninguno: el trabajo se
> llevaba en el contexto de la sesión, y el operador lo señaló. Un plan que vive en
> la memoria de un agente se pierde en el primer `/clear`.

---

## P0 — Regresiones que YO metí en `main` y que CI no puede ver

### 1. Una claim PINEADA se descarta por el filtro de relevancia cero
**Estado:** ABIERTO · **Origen:** PR #223 (`50f54fc`) · **Severidad:** alta

`tests/test_vector_search.py::TestHybridRetrieval::test_pinned_claims_always_survive`
falla en `main`. Pinear es una acción explícita del operador que significa *esto se
queda siempre*; el filtro que agregué en #223 ("una fila con relevancia cero no es un
resultado") la tira igual.

Son dos reglas correctas en conflicto, y la del operador tiene que ganar: el filtro
necesita una exención para `pinned`. No es una expectativa vieja — es un pedido
explícito que el sistema dejó de honrar.

**Verificación:** el test pasa en `50f54fc~1` y falla en `origin/main`.

### 2. El contrato de pesos del score híbrido cambió sin actualizar su documentación
**Estado:** ABIERTO · **Origen:** PR #223 · **Severidad:** media

`test_hybrid_score_components` documenta la fórmula `0.30*lex + 0.20*conf +
0.10*fresh + 0.40*vec`. El floor vectorial (`VECTOR_RELEVANCE_FLOOR = 0.65`) reescala
el término vectorial, así que con `vec=0.8` el aporte real es `0.40 * (0.8-0.65)/0.35`
en vez de `0.40 * 0.8`. La diferencia medida (0,1486) coincide exacto con el fallo.

El cambio es intencional y defendible; lo que falta es actualizar el contrato y su
test para que digan lo que el código hace.

### 3. HUECO ESTRUCTURAL: CI no corre NINGUNO de los 97 tests `ml`
**Estado:** ABIERTO · **Severidad:** alta — es la causa de que 1 y 2 se mergearan

`.github/workflows/ci.yml:28` corre `pytest tests/ -m "not ml"`. Hay **97 tests**
marcados `ml` (embeddings/vector/Qdrant) y **ninguno** se ejecuta en ningún job.
Corridos a mano el 2026-08-19: **95 pasan, 2 fallan** — los dos de arriba.

Es la décima señal inerte del día y la de mayor alcance: una categoría entera de
tests que se lee como cobertura y no corre nunca. Decidir entre un job `ml` opcional
(con las dependencias pesadas) o al menos una corrida programada; sin eso, todo el
subsistema vectorial queda sin red.

---

## P1 — Assessment de OpenViking (`artifacts/2026-08-19-openviking-assessment.html`)

| # | Ítem | Estado |
|---|---|---|
| 4 | Restaurar el arnés LongMemEval | **HECHO** — PR #224, corrido sobre 500 preguntas |
| 5 | Acumulador `zero_result_rate` sobre tráfico real | **HECHO** — sin commitear al escribir esto |
| 6 | Adaptador LoCoMo (segundo benchmark) | pendiente |
| 7 | Trayectoria de retrieval pegada al resultado | a evaluar |
| — | Capas L0/L1/L2, `viking://`, redacción reversible, hotness sigmoidea | RECHAZADOS con motivo |

---

## P2 — Metas del marcador (`scripts/probes/goals.json`)

| Meta | Estado |
|---|---|
| G2 honestidad, G3 relevancia, G4 índice | cumplidas, en sostener |
| **G5 inhallables** | contra-métrica en ROJO a 84,7 vs 86,0 — **gate del operador** |
| **G1 alcanzabilidad** | 60,7 vs 66,0; falta decidir si es techo de la cohorte o defecto |

**Sobre G1:** LongMemEval subió `hit@1` de 0,342 a 0,506 y `hit@5` de 0,430 a 0,646
mientras G1 no se movió. Esa divergencia sugiere que el techo es una propiedad de la
cohorte, no del recuperador — pero **hay que medirlo, no deducirlo**.

---

## P3 — PRs abiertos

| PR | Qué | Estado |
|---|---|---|
| #224 | Arnés LongMemEval, cableado y fijado | ubuntu verde, windows corriendo |
| #219 | `CODEBASE_REVIEW.md` (auditoría Gemini/Jules) | sin revisar |
| #168 | `CREDITS.md` + watchlist de prior art | 51 días abierto; **bloquea el plan de relevamiento** |
| #185 | Seguridad fase 1 | borrador de 30.893 líneas, 37 días parado |

---

## P4 — Gates del operador (NO avanzar sin su palabra)

- **T-0179** — cola del steward: 220 propuestas sin consumidor, la más vieja de 4 meses.
- **Umbral de G5** — fijarlo es `evaluator-edit`; quien persigue la meta no elige el número.

---

## Hallazgos del día que ya están cerrados

Diez señales inertes: `graph.json` sin lector · el marcador midiendo `site-packages` ·
el floor gate apagándose solo · `check_probes_frozen.py` sin caller en CI · ese mismo
guarda neutralizable desde el PR · bypass por rename · el test del desempate vacuo ·
el arnés de LongMemEval fuera de `main` · el arnés sembrando cero y reportando 0,000 ·
y los 97 tests `ml` que CI nunca corre (ítem 3, abierto).

**El patrón:** un mecanismo que se lee como si funcionara y calla. Su ausencia de
señal es indistinguible de que todo anda bien, y por eso ninguno se detectó solo.
