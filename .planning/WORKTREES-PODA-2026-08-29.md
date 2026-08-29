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
  - `chore/test-reliability` (1 commit — aísla los tests ML tras el marcador `ml`)
  - `worktree-agent-a74a6cfd48d80093d` (3 commits — neighbor walk sobre
    `claim_entity_links`, `recompute_tiers` en psycopg, revivir claims archivadas
    en el dedup por content-hash)

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
