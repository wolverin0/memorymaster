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
**Estado:** CERRADO (`55787c6`) — exenta del filtro; 12 tests del guard siguen verdes · **Origen:** PR #223 (`50f54fc`) · **Severidad:** alta

`tests/test_vector_search.py::TestHybridRetrieval::test_pinned_claims_always_survive`
falla en `main`. Pinear es una acción explícita del operador que significa *esto se
queda siempre*; el filtro que agregué en #223 ("una fila con relevancia cero no es un
resultado") la tira igual.

Son dos reglas correctas en conflicto, y la del operador tiene que ganar: el filtro
necesita una exención para `pinned`. No es una expectativa vieja — es un pedido
explícito que el sistema dejó de honrar.

**Verificación:** el test pasa en `50f54fc~1` y falla en `origin/main`.

### 2. El contrato de pesos del score híbrido cambió sin actualizar su documentación
**Estado:** CERRADO (`55787c6`) — el assert deriva de `_vector_above_floor` y verifica la atenuación · **Origen:** PR #223 · **Severidad:** media

`test_hybrid_score_components` documenta la fórmula `0.30*lex + 0.20*conf +
0.10*fresh + 0.40*vec`. El floor vectorial (`VECTOR_RELEVANCE_FLOOR = 0.65`) reescala
el término vectorial, así que con `vec=0.8` el aporte real es `0.40 * (0.8-0.65)/0.35`
en vez de `0.40 * 0.8`. La diferencia medida (0,1486) coincide exacto con el fallo.

El cambio es intencional y defendible; lo que falta es actualizar el contrato y su
test para que digan lo que el código hace.

### 3. HUECO ESTRUCTURAL: CI no corre NINGUNO de los 97 tests `ml`
**Estado:** CERRADO (`55787c6`) — job `ml` en ci.yml. Corrida: 97/97 pasan
**Seguimiento CERRADO (`35fec68`):** los 7 tests de ranking puro viven en `tests/test_hybrid_ranking_contract.py` sin marcador y corren en la matriz completa. Evidencia: `-m "not ml"` 4658 passed · `-m ml` 90 passed.
**El marcador `ml` del resto SE QUEDA:** el motivo real (pytest.ini) es SIGSEGV/cuelgue al MEZCLAR en la corrida completa sobre Windows, no dependencias ausentes. Que pasen con torch bloqueado no lo refuta — ese experimento corre la configuración segura · **Severidad:** alta — es la causa de que 1 y 2 se mergearan

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
| 5 | Acumulador `zero_result_rate` sobre tráfico real | **HECHO** (`55787c6`) — tool MCP `recall_stats`, 6 tests, verificado por mutación |
| 6 | Adaptador LoCoMo (segundo benchmark) | pendiente — **gate**: necesita bajar otro dataset; la autorización previa era para LongMemEval y no se extiende |
| 7 | Trayectoria de retrieval pegada al resultado | **HECHO** (`5b289cb`) — `drop_trace`: razón canónica por descarte, apagado por defecto. 6 tests, mutación verificada |
| — | Capas L0/L1/L2, `viking://`, redacción reversible, hotness sigmoidea | RECHAZADOS con motivo |

---

## P2 — Metas del marcador (`scripts/probes/goals.json`)

| Meta | Estado |
|---|---|
| G2 honestidad, G3 relevancia, G4 índice | cumplidas, en sostener |
| **G5 inhallables** | contra-métrica en ROJO a 84,7 vs 86,0 — **gate del operador** |
| **G1 alcanzabilidad** | 60,7 vs 66,0; falta decidir si es techo de la cohorte o defecto |

**Sobre G1 — MEDIDO (`scripts/diagnose_g1_ceiling.py`), ya no deducido:**

De 116 fallos, **67 son ambigüedad legítima**: el ganador contiene los dos tokens de
la consulta tan bien como la claim buscada, así que ninguna elección es incorrecta.
Con una consulta de 2 tokens raros sobre 24.821 claims eso es inherente a la sonda,
no un defecto del recuperador. **Techo alcanzable ≈ 84-89%**, o sea que el objetivo
de 66 SÍ es alcanzable.

Dos hipótesis mías que la medición **refutó**:
- *"El `session_diversity_cap` se come 44 objetivos"* → desactivándolo: 61,3 → **61,0**,
  dentro del ruido de 0,7. Esas claims ya venían perdiendo; el descarte era corriente
  abajo, no la causa.
- *"Son claims duplicadas"* → los duplicados exactos son 213 claims, **0,9% de la base**.
  No explican 85 fallos. Vi un ejemplo y generalicé.

Lo que queda arreglable son ~30 casos de orden/truncación, no un defecto estructural.

---

## P3 — PRs abiertos

| PR | Qué | Estado |
|---|---|---|
| #227 | `list_claims` ignoraba el filtro `ids` | **abierto** — 4697 tests verdes local (`530c97d` + verdad de release regenerada); esperando CI |
| #226 | Migración GLM → Gemini/OAuth | **mergeado** (squash) |
| #224 | Arnés LongMemEval + 2 regresiones de main + hueco de CI | **mergeado** |
| #219 / #168 / #185 | auditoría Jules · CREDITS · seguridad fase 1 | **cerrados** — #185 tras probar que sus 93 archivos ya estaban en main |

---

## P3.1 — La superficie MCP acepta parámetros inventados (abierto, rama aparte)

Medido, no estimado: **51 de 51 herramientas** MCP tienen el schema sin
`additionalProperties: false`, así que un parámetro que no existe se descarta en silencio
en vez de rechazarse. Es la causa de clase detrás del bug de `ids` (#227): no había filtro
que fallara, había un argumento que se evaporaba.

Confirmado en `list_claims`: `scope` ni siquiera es parámetro del endpoint, y pedir un scope
inexistente devuelve **todas** las claims. El resultado ampliado se lee como legítimo.

Dónde muerde de verdad — cuando el parámetro que se evapora es el que restringe:

| Tool | Param | Default | Si se evapora |
|---|---|---|---|
| `archive_by_source` | `dry_run` | `True` | previsualiza — dirección segura |
| `forget` | `apply` | `False` | no aplica — dirección segura |
| `compact_memory` | `retain_days` | `30` | pedir retener **más** retiene **menos** |
| `resolve_steward_proposal` | `apply_on_approve` | `True` | pedir **no** aplicar **aplica** |

Los dos últimos caen para el lado inseguro. Requiere que el llamador escriba mal un nombre
declarado — que es exactamente lo que pasó con `ids`: un nombre plausible que no existía.

**Cuidado al arreglarlo:** poner `additionalProperties: false` en 51 herramientas convierte
en error lo que hoy se ignora. Hay que medir qué llamadores reales pasan parámetros de más
antes de endurecerlo, o el arreglo rompe a quien hoy funciona por accidente.

---

## P4 — Gates del operador (NO avanzar sin su palabra)

- **Memoria de solo escritura en 9 de 10 panes.** El `CLAUDE.md` raíz de `Py Apps` manda
  ingestar con `scope: project:py-apps` en todo el árbol, justamente contra la
  fragmentación. Pero el allowlist de lectura se deriva del workspace, y **ningún** pane
  anidado resuelve a `project:py-apps`: cada uno ve solo su propio slug. Medido pane por
  pane — wezbridge, rifas, mutual, infra, yolo26, crm, frontendesigner, memorymaster,
  whatsappbot — **9 de 10 no pueden leer lo que la instrucción les dice que escriban**.
  Son **2974 claims vivas** en `project:py-apps` más **3901** en `project:whatsappbot-final`.

  Detrás hay una asimetría real del código: `canonicalize_slug` corta sufijos de canal
  (`-final`, `-prod`) al **leer**, pero `ingest` con `scope` explícito guarda el string
  **crudo**. Verificado: `scope='project:whatsappbot-final'` se almacena tal cual, mientras
  ese mismo workspace lee `project:whatsappbot`. Escribe en un lugar, lee en otro.

  Y no da señal: el recall devuelve las claims del scope propio, así que se lee como si
  funcionara. Otra inerte, la más cara hasta ahora.

  **Escape verificado — no se perdió nada.** Es "solo escritura" en el camino por defecto,
  no en absoluto. Por la herramienta MCP real, desde el workspace de whatsappbot-final:
  `query_memory(scope_allowlist='project:py-apps', trust_mode='exploratory')` devuelve
  `[133058, 133012, …]`. Hacen falta las **dos**: `scope_allowlist` (no `scope`, que en
  `query_memory` no existe y se descarta) y `exploratory` (el recall confiable es
  confirmed-only por diseño, y lo recién ingestado es `candidate`). Eso baja la urgencia sin
  tocar la gravedad. La memoria basada en archivos (`MEMORY.md`) es independiente y funciona.

  La trampa concreta detrás del tropiezo: **`search_verbatim` usa `scope` y `query_memory`
  usa `scope_allowlist`**. Se aprende uno, se yerra el otro, y el yerro se descarta en vez
  de fallar — el costo real de que ningún schema declare `additionalProperties: false`.

  **Tres decisiones que son del operador, no mías:** cuál es el scope canónico (hoy el repo
  dice uno y el canonicalizador otro); si el resolver debe incluir los scopes ancestros en
  el allowlist —eso cambia el recall de toda la flota—; y si se re-scopean las claims ya
  escritas. Ninguna se toca sin su palabra.

- **T-0179** — cola del steward: 220 propuestas sin consumidor, la más vieja de 4 meses.
- **Umbral de G5** — fijarlo es `evaluator-edit`; quien persigue la meta no elige el número.

---

## Hallazgos del día que ya están cerrados

Diez señales inertes: `graph.json` sin lector · el marcador midiendo `site-packages` ·
el floor gate apagándose solo · `check_probes_frozen.py` sin caller en CI · ese mismo
guarda neutralizable desde el PR · bypass por rename · el test del desempate vacuo ·
el arnés de LongMemEval fuera de `main` · el arnés sembrando cero y reportando 0,000 ·
y los 97 tests `ml` que CI nunca corría (ítem 3, ya cerrado).

**El patrón:** un mecanismo que se lee como si funcionara y calla. Su ausencia de
señal es indistinguible de que todo anda bien, y por eso ninguno se detectó solo.
