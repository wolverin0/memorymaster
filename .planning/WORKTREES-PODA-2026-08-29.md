# Poda de worktrees ejecutada — 2026-08-29

> Qué cubre: la poda de los 24 worktrees de memorymaster, autorizada por el operador,
> con la remediación previa que la hizo segura. Términos clave: worktree, poda,
> trabajo único, remediación, `git worktree remove`.
> Leer cuando: se dude si se perdió trabajo, o antes de volver a crear worktrees.

**Autorización:** explícita del operador ("todos los worktree que haya debemos
remediarlo y eliminarlos una vez hecho"). El inventario del 24-08 los había dejado
sin podar porque la decisión era suya.

## Resultado

**24 → 2.** Quedan el canónico (`Py Apps/memorymaster`, main) y `mm-230`
(`fix/recall-reaches-ancestor-scopes`, PR #230 abierto — se quita al mergear).

## La remediación, que es lo que la hizo segura

El riesgo documentado el 24-08 era perder archivos sin commitear: una poda previa
hizo desaparecer 111, y uno era el script del digest semanal invocado por ruta desde
una tarea programada, que quedó en exit 2.

Esta vez se midió antes de tocar nada:

- **Archivos sucios: CERO en los 24.** El riesgo de 2026-08-24 no aplicaba.
- **Trabajo único (`rev-list origin/main..`)**: 10 ramas con commits propios.
- De esas 10, **8 ya estaban en origin al día**. Las **2 que no**, se pushearon
  ANTES de podar:
  - `worktree-agent-a74a6cfd48d80093d` (3 commits — neighbor walk sobre
    `claim_entity_links`, `recompute_tiers` en psycopg, revivir claims archivadas
    en el dedup por content-hash). **Confirmado con `git cherry`: 3 no aplicados.**
  - `chore/test-reliability` (1 commit — aísla los tests ML tras el marcador `ml`).
    **CORRECCIÓN (mismo día):** `git cherry origin/main` da **0 no aplicados** —
    su contenido ya estaba en main. Pushearla fue innecesario e inofensivo, pero
    la afirmación original de que tenía trabajo en riesgo era falsa.

## Por qué se preservó en vez de decidir

Un pane par midió con `git cherry` (patch-id) y concluyó que sólo una rama tenía
trabajo real. **Ese número no reprodujo**: al menos cuatro dan no-cero acá.

Y el fondo es que **ninguno de los dos criterios zanja el squash-merge**:

- *alcanzabilidad* (`rev-list origin/main..`) sobrecuenta porque el squash deja los
  commits originales inalcanzables aunque su contenido esté en main;
- *patch-id* (`git cherry`) también sobrecuenta, porque el squash combina N commits
  en **uno solo** cuyo patch-id no coincide con ninguno de los originales.

Por eso la remediación fue **preservar, no juzgar**: pushear la rama a origin cuesta
cero y es correcta bajo cualquier criterio. Bajo incertidumbre, esa asimetría entre
el costo de guardar y el costo de perder es toda la decisión.

**Quitar un worktree no borra su rama.** Todo el trabajo sigue existiendo como rama,
y ahora además está en GitHub.

## Qué eran los 24

- 6 en `_worktrees/` — ramas de agosto ya en origin (T-0121, staleness, sensitivity,
  intake, v47-acceptance, release/v4.7.1)
- 9 en `finalorchestra-runtime-jobs/` — clones por job del piloto D-006 de hoy,
  descartables por diseño
- 4 en `.claude/worktrees/` — agentes y workflows viejos
- 3 en `Temp/` (mmchk, mmpre, mmpre2) — detached, sin trabajo único
- `mm-230`, el único vivo

## Por qué importa

Los worktrees eran la causa raíz de que el checkout canónico se desactualizara solo:
el trabajo se hace en el lateral, el PR mergea en GitHub, y el canónico nunca baja
el resultado. Nada avisaba. Desde el 27-08 eso además lo vigila
`MM-freshness-sentinel`, y desde hoy el gate de aceptación vigila que el sentinel
corra.
