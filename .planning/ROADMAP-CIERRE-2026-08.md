# ROADMAP DE CIERRE — memorymaster, agosto 2026
# Qué cubre: los 4 ítems restantes con criterio de salida VERIFICABLE cada uno, y la
# cláusula de modo-mantenimiento que corta el trabajo autogenerado infinito.
# North star: el operador habla / pinea / lee el digest — y la memoria le sirve sin
# que la administre. Leer cuando: alguien pregunte "qué falta" o quiera agregar trabajo.
# Regla dura: NO se agregan ítems a este archivo sin palabra del operador.

## Los 4 ítems que faltan para decir LISTO

- [~] **1. Wiki curada viva.** HECHO el 2026-08-27: vault vaciado (backup de los 122
  viejos en `.claude/_backups/wiki-pre-clean-20260827.tgz`), subjects backfilleados
  con los temas curados (undo en scratchpad), regenerado solo-pins: 12 artículos
  temáticos + índice/bases, organizados por proyecto. 5 temas singleton quedan fuera
  (regla ≥2 claims/artículo del motor; siguen pineados en recall). Carpeta abierta.
  *Falta solo: que el operador diga "la vi".*

- [x] **2. Reglas calibradas aplicadas.** HECHO el 2026-08-27, autorizado explícito
  del operador: bloque de testing-por-fase en `~/.claude/CLAUDE.md` (Verification) y
  dual-mode git en `~/.claude/rules/git-workflow.md`. Backups `.bak-20260827` al lado.

- [~] **3. Canónico reconciliado + sentinel verde real.** HECHO el 2026-08-27: los
  tres PRs mergeados con CI verde — #232 (sentinel, 2447a41), #234 (gate de tenant,
  b677091), #233 (rulings de producción, 20ba55e). Canónico en `main` al día en
  20ba55e, árbol limpio, sentinel manual verde en silencio, `--pinned-only` por fin
  vive en main. El split-brain que originó la tarjeta quedó cerrado: main ya no
  corre detrás del paquete instalado.
  *Falta SOLO: la corrida programada del sentinel de mañana 08:37 con Last Result 0
  — prueba que CORRE, no solo que existe, y cierra mm-b234 sin trabajo nuevo.*

  Camino real (para el próximo que lea): #233 necesitó 3 vueltas de CI. El rojo no
  era flake — `test_dashboard_data_endpoints` asertaba la semántica VIEJA de la cola
  (≥1 item con solo conflicted/stale), y la rama había cambiado el contrato a "cola
  humana = solo del operador" sin actualizar a su testigo. Se ancló el test a las dos
  ramas del contrato: vacía sin pins, poblada al pinear.

- [x] **4. Deudas del handoff viejo: cerradas o declaradas wontfix.** Veredictos
  (operador, 2026-08-27, vía AskUserQuestion):
  - (a) **HECHO** — `git rm --cached obsidian-vault` + .gitignore + commit en los 8
    repos (bajoneando, brlite, damore2, interonda, mutual, pather, venezia, yolo26).
    Disco intacto.
  - (b) **HECHO (fix mínimo)** — PR #234: el gate de tenant nombra la frontera y
    ambos tenants en vez de mentir "does not exist" sobre una claim visible.
    El gate en sí NO se rediseñó: escribir el corpus histórico sigue vedado.
  - (c) **DECLARADO** — la política PPR-7 actual (solo evidencia gobernada alimenta
    el grafo, fail-closed, 0,36% del corpus) SE MANTIENE como decisión. No es un
    bug pendiente: es el diseño elegido. Embudo medido en claim 134913 / mm-41f4.
  - (d) **WONTFIX** — MM2 (forense split-brain) y MM5 (checkpoint hash chain) del
    grill: enterrados. Si un incidente real los reclama, renacen como tarea nueva
    con evidencia, no como deuda perpetua.

## Cláusula de cierre (la que corta el infinito)

Cuando los 4 estén marcados, este proyecto entra en **MODO MANTENIMIENTO** y se
declara LISTO. En modo mantenimiento:

- Lo automático sigue solo y en silencio: steward 6h, curation-drain, dreaming shadow,
  sentinel, digest semanal. Máquinas manteniendo máquinas, sin chat.
- Este pane NO acepta tareas autogeneradas por la flota (rulings, follow-ups de
  follow-ups, mejoras especulativas) sin que el operador las apruebe una por una.
  "El ledger la despachó" no es aprobación del operador.
- El único trabajo nuevo legítimo: lo que el operador pida, o algo ROTO con evidencia
  (un check en rojo, no una oportunidad de mejora).

El infinito no se termina completándolo. Se termina declarando qué es suficiente.
