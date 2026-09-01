# Minado de reglas / linaje — estado al detenerse, 2026-09-01
# Que cubre: hasta donde llego el barrido, que entro a la base, y por que se
# freno. Leer ANTES de retomar el minado: el watermark persiste, retomar
# continua desde donde quedo y no repite trabajo.
# Estado: DETENIDO por decision del operador. Nada corriendo.

## Donde quedo

| | |
|---|---|
| Watermark alcanzado | **9.821.662** |
| Tope de `verbatim_memories` | 10.190.315 |
| Barrido | **2.261 filas de 370.914** — 0,6% |
| `rule_observations` | **184 filas** (empezo en 0) |
| Raices distintas | **3** |
| Fingerprints distintos | 184 |
| Reglas con >=3 raices independientes | **0** |
| Claims totales | 147.785 · `quick_check ok` |

Scopes tocados: `project:mzcopilot` 87, `project:whatsappbot-final` 86,
`project:omniremote` 11.

## Por que se freno

No fue un error. El operador lo corto tras ver el costo proyectado, y la
proyeccion no se pudo dar con confianza: dos lotes contiguos de 400 ventanas
midieron **21,8 min / 302 llamadas** y **0,5 s / 0 llamadas**. Varianza de
2.600x. El costo lo manda la densidad de correcciones del tramo, no la cantidad
de filas, asi que cualquier "faltan N horas" desde n=2 seria inventado.

El dato que decidio: el 0,6% barrido metio ~343 claims de tipo regla al corpus
gobernado. Sostenida esa densidad, drenar el resto agrega decenas de miles a un
corpus de 147k. Es un cambio material de la memoria, no un costo de computo.

## Lo que el ejercicio si dejo claro

**El liston funciona y drenar mas no lo mueve solo.** Con 184 observaciones y
apenas 3 raices, NINGUNA regla alcanza las 3 raices independientes que exige un
candidato. Lo que hace falta no es mas filas: es **diversidad de raices**, que
depende de cuantas sesiones distintas toque el barrido, no de cuantas filas coma.

Corolario para quien retome: barrer secuencialmente desde el watermark recorre
sesiones contiguas, o sea que concentra raices en vez de diversificarlas. Si el
objetivo es conseguir candidatos, muestrear tramos SEPARADOS del corpus rinde
mas por llamada que seguir de largo.

## Gotcha operativo (costo real hoy)

`drain_rules.py` lanza `mine-rules` como SUBPROCESO. Matar el driver **no** mata
al hijo: despues del kill las observaciones siguieron subiendo 148 -> 178 -> 184.
Hubo que buscarlo por linea de comando y matarlo aparte. Quien automatice esto
tiene que matar el arbol, no el padre.

## Como retomar

`python -m memorymaster --db memorymaster.db mine-rules --limit N --provider google`

El watermark persiste en la base: continua desde 9.821.662 sin repetir. Cada
lote deja filas y watermark commiteados, asi que una interrupcion cuesta el lote
y no el trabajo. Driver por lotes con medicion de ritmo en el scratchpad de la
sesion (`drain_rules.py`).
