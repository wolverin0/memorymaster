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

---

## Muestreo por tramos separados — resultado (misma fecha)

Hipotesis a probar: como el liston pide 3 raices INDEPENDIENTES y barrer lineal
recorre sesiones contiguas, saltar entre tramos separados deberia diversificar
raices mas rapido por llamada.

**FALSADA, y la razon importa mas que el resultado.**

| Tramo | Desde id | Llamadas | Ingeridas | Obs total | Raices | Candidatos | Seg |
|---|---|---|---|---|---|---|---|
| 1 (8%) | 815.225 | 58 | **0** | 191 | 3 | 0 | 258 |
| 2 (24%) | 2.445.675 | 99 | **0** | 199 | 3 | 0 | 434 |
| 3 (41%) | 4.178.029 | 99 | **0** | 200 | 3 | 0 | 371 |
| 4-7 | — | — | — | — | — | — | cancelados |

**Costo medido: 256 llamadas al modelo -> 16 observaciones nuevas -> CERO raices
nuevas.** Ese es el numero que deberia mirar quien quiera reintentarlo.

`ingeridas = 0` en los tres tramos confirma la mitad de la hipotesis que SI era
cierta: re-minar territorio ya barrido no duplica claims (la idempotency key las
dedupea) pero si registra linaje. Lo que fallo fue la otra mitad — el linaje
nuevo salio de las MISMAS 3 sesiones.

Por que fallo: **202 observaciones salieron de 3 sesiones**. El minado no
reparte por sesion — toma ventanas contiguas, y una sesion larga aporta decenas
de reglas. Cada tramo tambien cae dentro de pocas sesiones, asi que saltar no
cambia nada. La diversidad de raices depende de cuantas SESIONES DISTINTAS
atraviesa el barrido, y con 120 ventanas se atraviesa una o dos.

### Una alarma que levante y era falsa

Vi 3 raices / 3 scopes en correspondencia 1:1 y casi reporto que
`root_session_hash` era constante por scope, o sea un defecto estructural que
haria el liston inalcanzable. **Lo medi antes de reportarlo**: las filas de
origen vienen de 3 sesiones REALES y hay 3 hashes. El linaje registra una raiz
por sesion, como debe. La correspondencia era coincidencia — esas 3 sesiones
pertenecen a 3 proyectos distintos.

### Conclusion para el liston

Conseguir 3 raices independientes por regla exige que la MISMA regla aparezca en
sesiones distintas: repeticion del operador a lo largo del tiempo, no mas
barrido. El gate no esta trabado; esta diciendo con precision que todavia no hay
evidencia repetida e independiente. Eso es informacion, no un bloqueo.

### Gotcha nuevo (costo real, segunda vez el mismo dia)

`mine-rules` escribe el watermark al terminar SIEMPRE, incluso con `--since-id`
(rule_miner.py:505). El muestreador lo salvaba y restauraba en un `finally`...
que **no corre si se mata el proceso**. Tras el kill el watermark estaba en
9.819.687; hubo que restaurarlo a mano a 9.821.662 (1 fila tocada, verificado).

Y la proteccion nacio rota: la clave del watermark estaba escrita a mano como
`rule_miner_watermark` cuando la real es `rule_miner.last_verbatim_id`. Con la
equivocada, leer devolvia 0 y restaurar hacia UPDATE sobre una clave inexistente
— cero filas tocadas, punto de reanudacion perdido, y el log afirmando que lo
habia restaurado. Se detecto midiendo el baseline en vez de confiar en el propio
codigo. Ahora la clave se IMPORTA del modulo y la restauracion revienta si no
toca exactamente una fila.
