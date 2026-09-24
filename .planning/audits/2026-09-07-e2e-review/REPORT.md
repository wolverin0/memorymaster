<!-- doc-head: completed bounded MemoryMaster E2E review; fixes proposed, not implemented -->
Covers: disposable lifecycle, installed MCP, Dreaming replay, context budgets, UI and operations.
Key findings: stranded extracted captures, inconsistent recall payloads, stale review PASS, weak Origin matching.
Evidence: 195 existing tests pass; four proposed acceptance contracts fail on version 4.8.9.
Read when prioritizing improvements; ROADMAP.md remains the only roadmap and live recovery needs approval.
<!-- /doc-head -->

# Revisión E2E — 7 de septiembre de 2026

## Resultado

El núcleo de memoria gobernada funciona en el recorrido de prueba: captura,
evidencia, promoción explícita, recall citado y retiro. El MCP instalado responde
y protege el acceso. Sin embargo, no corresponde declarar sana toda la aplicación:
Dreaming puede abandonar trabajo diferido sin reportar error y la supervisión puede
conservar un PASS después de una ejecución terminada por timeout.

Priorizaría **reanudar Dreaming, corregir el contrato de recall y hacer confiable
el estado operativo**. Después: endurecer Origin, medir utilidad y simplificar la
interfaz. No hace falta otro framework, otro modelo ni una reescritura general.

Revisión terminada; correcciones, recuperación histórica y despliegue no realizados.

## Alcance y mapa del sistema

Base: `main`, commit `b0dd976a32f8d29fb6ebf8257b4a9b8c5bcde700`, versión 4.8.9,
sin retraso respecto de `origin/main` al comprobarlo. El paquete instalado también
declara 4.8.9. Se preservaron los directorios ajenos `delta-exchange/` y `repos/`.

La fachada `remember/recall/forget/improve` expone el producto. Captura conserva
fuentes/evidencia; Dreaming usa un ledger auxiliar para extracción y consolidación.
Los candidatos y su ciclo autorizado residen en SQLite. El steward decide las
transiciones; ni un extractor ni una observación sustituyen esa autoridad.

Recall filtra y rehidrata claims autorizados, luego ordena y empaqueta contexto.
Perfil y observaciones son proyecciones derivadas con soportes verificables, no
fuentes independientes. Su activación y su utilidad son preguntas distintas.

Las superficies son CLI, MCP stdio/HTTP y un dashboard HTTP con HTML/JS embebido.
Windows Task Scheduler ejecuta los procesos periódicos; sus códigos de salida,
los artefactos de revisión y la entrega a un agente son evidencias diferentes.

Inventario en [inventory.json](inventory.json): 396 archivos Python de producto,
91.159 líneas físicas, 470 archivos Python bajo tests, 51 herramientas MCP,
23 archivos de migración hasta la versión 24 y 225 claves literales de entorno.
El dashboard registra 28 GET estáticos, un patrón GET de lineage y cuatro POST;
MCP HTTP tiene health, readiness y el transporte MCP. Los 46 paths literales
candidatos del inventario no son 46 endpoints independientes.
Los siete flags detectados por helpers son un límite inferior. El inventario
automático no significa que se hayan revisado manualmente las 91.159 líneas.

## Evidencia ejecutada

| Capa | Resultado observado | Límite de la conclusión |
|---|---|---|
| Ciclo y límites de confianza | 119 pruebas aprobadas: demo, fachada, autorización/tenant, MCP HTTP, dashboard auth, graph, perfil y Dreaming | Proveedores de prueba; no precisión del LLM real |
| Regresiones adicionales | 55 aprobadas: packing, provider packing, perfil, captura/dashboard, revisión operativa y UX de gobernanza | No suite completa ni matriz de plataformas |
| Recuperación y migraciones | 21 aprobadas, incluyendo backup cifrado y restore con integridad/FK en fixture | No restore de un backup productivo/NAS actual |
| Contratos nuevos | Cuatro fallos reproducibles, descritos abajo | Están en artifacts, no se incorporaron al gate normal |
| MCP instalado | health 200, ready 200, petición sin token 401, 51 tools, query autorizada con una fila | Una muestra de recall de 5,446 s, no p95 ni benchmark |
| Hook y perfil | Matcher instalado `startup|resume|compact`; proyección exacta y marcador generado | No se forzó un evento real de compact/resume en otro agente |
| Navegador Chrome | Dashboard temporal muestra tres candidatos, búsqueda y lineage del claim sintético | Solo desktop/fixture; no barrido visual completo ni UAT móvil |

Los resultados están bajo [artifacts/e2e-review-20260907](../../../artifacts/e2e-review-20260907/):
`lifecycle-tests.xml`, `extended-tests.xml`, `recovery-tests.xml`, `red-contracts.xml`,
`installed-live.json`, `live-metadata.json`, `runtime-supplement.json`,
`recall-reproduction.json`, `csrf-reproduction.json` y `scheduler-events.json`.
Ruff pasó para todos los scripts nuevos de revisión.

## Propuestas priorizadas

P1 = corregir antes de aumentar el uso automático; P2 = siguiente mejora acotada;
P3 = experimento opcional. Son prioridades de producto, no puntuaciones CVSS.

| ID | Prioridad | Cambio mínimo propuesto | Beneficio |
|---|---|---|---|
| E2E-01 | P1 | Hacer reanudable la consolidación diferida | El trabajo ya extraído no queda varado |
| E2E-02 | P1 | Unificar selección y medición del payload de recall | El presupuesto representa lo entregado |
| E2E-03 | P1 | Separar último intento de último resultado completo | Un timeout no deja apariencia de salud actual |
| E2E-04 | P2 | Comparación exacta de Origin y defensa local explícita | Elimina aceptación de hosts parecidos |
| E2E-05 | P2 | Conectar evaluación de utilidad a la operación existente | Distingue generación correcta de memoria útil |
| E2E-06 | P2 | Resumen orientado a acciones y búsqueda explicable | Reduce diagnóstico manual y falsos vacíos |
| E2E-07 | P3 | Perfil MCP pequeño por cliente, opt-in y compatible | Menos superficie que descubrir en tareas normales |

### E2E-01 — Dreaming no retoma las capturas diferidas

**Hecho reproducido.** `DreamLedger.eligible()` selecciona solamente `captured`
y `retryable` (ledger.py:195-207). `defer_consolidation()` deja `extracted`
(ledger.py:235), y `DreamWorker.run()` obtiene el próximo trabajo a través de
ese selector (worker.py:144-148). El paquete instalado tiene la misma exclusión.

Un fixture agota el presupuesto de consolidación, conserva la extracción y ejecuta
otro ciclo al día siguiente con presupuesto disponible. El segundo devuelve
`ok=true`, cero errores y cero aplicaciones; la captura sigue en `extracted`.
La prueba existente de presupuesto verifica el aplazamiento, pero termina antes
de comprobar que se pueda retomar (`tests/test_dreaming_worker.py:206`).

**Alcance vivo.** Hay 345 capturas `extracted`: 338 con propuestas, 1.052 payloads
de candidato y ninguna con decisiones persistidas. 324 se asocian a runs de aplicación
y 21 a dry-run. Seis se actualizaron en las últimas 24 h y los runs registraron seis
aplazamientos por presupuesto. No atribuyo retrospectivamente el origen de las 345
a una sola causa; sí están fuera del selector actual. Los datos siguen guardados:
esto es trabajo varado, no pérdida física demostrada ni 1.052 memorias válidas.

**Propuesta.** Reanudar desde extracción persistida bajo el mismo lease, scope,
presupuesto y claves de idempotencia. Contabilizar antigüedad y motivo de aplazamiento.
Preservar candidate-first y no reconsolidar automáticamente todo el histórico.
Antes de cualquier recuperación: preview de IDs, dry-run/aplicación de origen,
fuentes retiradas, vigencia y scopes; la selección histórica requiere aprobación.

**Aceptación.** Dos ciclos en días distintos: una extracción total, consolidación
al renovarse el presupuesto, aplicación única y replay sin duplicados. Incluir
caída después de persistir extracción, entradas vacías y aislamiento por scope.
El contrato `test_deferred_consolidation_resumes_next_day_without_reextracting`
debe pasar antes de proponer recuperación productiva.

### E2E-02 — El presupuesto y el recibo de recall no describen la misma entrega

**Hecho reproducido.** `pack_context()` estima bloques separados y una constante
de framing, pero serializa JSON indentado; devuelve todas las `ranked_rows`, no
solo las incluidas (context_optimizer.py:409-445). La fachada construye `claims`
desde esas filas (public/v1.py:439), y MCP incluye la fachada completa.

Con presupuesto 256, el JSON sintético declara 240 tokens y mide 280 con el mismo
estimador de la aplicación. Incluye dos claims, mientras `rows` contiene seis.
En otro fixture de fachada, el bloque contiene un claim y el recibo tres:
presupuesto 256, tokens declarados 190, bloque estimado 228 y recibo completo 745.
Son estimaciones por caracteres, no mediciones de un tokenizer de proveedor.
No se demostró fuga entre scopes: el fallo es selección/presupuesto y duplicación.

**Propuesta.** Una sola selección final, filas y citas alineadas con ella; medir
la representación serializada final. Definir explícitamente si el presupuesto
cubre el bloque o todo el recibo MCP y publicar ambos tamaños si corresponden.
Resolver presupuestos menores al framing mediante contrato explícito, no metadatos
que aparenten cumplirlos. No exigir un tokenizer nuevo para corregir esta inconsistencia.

**Aceptación.** Contratos `test_json_budget_measures_serialized_output` y
`test_packed_rows_match_rendered_claim_ids`; agregar fachada/MCP, formatos text/XML/JSON,
Unicode, citas largas, casos vacíos y presupuesto mínimo. IDs entregados deben
coincidir con los renderizados y ninguna memoria excluida debe reaparecer en el recibo.

### E2E-03 — La última ejecución falló, pero el último JSON sigue en PASS

**Hecho vivo.** El intento de Operational Review comenzó a las 21:51:14 UTC y fue
terminado a las 22:16:14 UTC. El evento 329 de Task Scheduler dice explícitamente
que excedió el tiempo permitido: 25 minutos. `latest.json` conserva el PASS de las
15:57:44 UTC. No se ejecutó otra revisión larga para sustituir esta evidencia.

El wrapper acumula stdout y publica al terminar; una terminación externa no deja
un resultado nuevo. Además, el instalador del repositorio todavía fija 15 minutos
(`scripts/install-windows-operational-review.ps1:43`), distinto del PT25M registrado.
No se conoce todavía qué fase consumió los 25 minutos; aumentar el límite otra
vez sin medir fases no es la propuesta principal.

Los checkpoints diario y semanal terminaron con código 4: el log confirma que no
había un pane coincidente. Eso es fallo de entrega del pedido, no prueba de que
el trabajo de revisión ocurrió. El último recibo observado es del 6 de septiembre.

**Propuesta.** Reutilizar la revisión existente: registrar inicio/run ID y avance
por fase, conservar por separado el último resultado completo y derivar el estado
actual de frescura + último intento. Alinear el instalador con la política decidida.
Para checkpoints, representar por separado solicitado, entregado y completado.

**Aceptación.** Simular timeout/interrupción en fixture: resultado anterior se
conserva como histórico, estado actual queda INCOMPLETE/TIMEOUT y nunca PASS.
Medir duración por fase y comprobar un disparo natural posterior; un relanzamiento
manual o un código de wrapper no reemplaza esa prueba.

### E2E-04 — Origin usa coincidencia parcial

**Hecho reproducido.** `check_csrf()` acepta que el `host:port` configurado aparezca
en cualquier parte de Origin/Referer (dashboard_auth.py:177). Con `localhost:8765`,
acepta `https://evil-localhost:8765`. El contrato de rechazo falla. El modo legacy
omite además CSRF por diseño; rechazar bind no-loopback no equivale a validar Origin.

No se demostró un bypass del bearer ni un ataque desde un navegador ajeno. La
comparación débil es un defecto concreto de una defensa adicional, no evidencia
para declarar el servicio remotamente comprometido.

**Propuesta/aceptación.** Parsear y comparar esquema, hostname y puerto exactos;
rechazar userinfo, sufijos/hosts parecidos y orígenes ajenos. Definir la política
para solicitudes sin Origin y para localhost legacy. Mantener 401/403, probar
origen legítimo y el puerto efectivo, sin cambiar credenciales reales.

### E2E-05 — Medir valor de la memoria, no solo actividad

**Hechos actuales.** En 24 h: 660 descubrimientos de graph terminaron `no_supports`,
sin observaciones activas; las tres existentes están archivadas. Esto puede ser
abstención correcta, no razón para bajar el umbral. El perfil tiene 52 facts activos,
50 renderizados y 1.386/1.400 tokens, manifiesto exacto y cero discrepancias de soporte.
El run de perfil está avanzando, con 337 mensajes de usuario pendientes dentro de
su target; no se lo clasifica como atascado por tener un watermark incompleto.

Dreaming registró seis add, quince ignore y una propuesta de supersesión en 24 h.
Sus proveedores contabilizaron 551.262 tokens de entrada y 17.604 de salida en
45 llamadas exitosas, más un error. Es actividad del ledger de Dreaming, no costo
total de toda la aplicación ni demostración de valor de cada llamada.

Ya existe una [evaluación fechada de 88 acciones](../../../artifacts/dreaming-review-20260905/REPORT.md):
32 potencialmente duraderas, 42 útiles si se conservan fecha y revalidación,
seis de bajo valor y ocho inciertas. Son juicios diagnósticos de IA sobre un intervalo
anterior, no ground truth humano ni evaluación de la cohorte posterior a los últimos cambios.
No propongo empezar otra evaluación desde cero ni volver a implementar campos temporales.

**Propuesta.** Reutilizar ese corpus y evaluador para una cohorte posterior: medir
entailment, vigencia, scope correcto, duplicados y utilidad al recuperar. Asociar
consumo/latencia a recuerdos aceptados y efectivamente usados. En graph mostrar
por qué faltan soportes; en perfil mostrar último avance y target, no solo fact count.

**Aceptación.** Cohorte versionada y separada de la anterior, citas verificables,
comparación con baseline y casos de abstención. Cualquier porcentaje de precisión
debe indicar quién etiquetó. No declarar calidad mejorada solamente porque pasan
tests o no hay errores de proveedor.

### E2E-06 — Hacer explícito qué necesita atención y por qué no aparece algo

**Observación de navegador.** La portada temporal empieza por Start/Stop Operator,
seguida de numerosos paneles técnicos. Muestra los tres candidatos. Buscar `SQLite`
devuelve cero resultados, sin explicar allí que trusted recall excluye candidatos;
el lineage sí muestra correctamente el candidato. La separación de confianza es
correcta; el estado vacío no la explica. El texto de resultados muestra literalmente
`&#183;` porque se asigna una entidad HTML a `textContent` (dashboard.py:1090).

**Propuesta.** Añadir arriba un resumen breve de última captura procesada, trabajo
realmente pendiente, último recall y revisión vigente. Priorizar revisión humana;
dejar Start/Stop y los contadores completos en controles avanzados sin eliminarlos.
Explicar resultados vacíos con exclusiones por status/scope y acceso al inspector,
sin mezclar candidatos con trusted recall. Usar un carácter separador real.

**Aceptación.** Recorrido de navegador con candidato visible pero no recuperable,
claim confirmado recuperable, cita visible y exclusión tras retiro. Estados sin
datos, error, pendiente y revisión vencida diferenciados; validación desktop y móvil.

### E2E-07 — Reducir descubrimiento innecesario en clientes MCP normales

**Observación.** Se anuncian 51 herramientas; ya existe la fachada de cuatro
verbos. No se midió cuánto ahorraría ocultar herramientas ni qué cliente depende
de las especializadas.

**Propuesta.** Experimento opt-in por cliente: fachada habitual y conjunto avanzado
explícito. No eliminar herramientas ni cambiar el default de clientes existentes.
Inventariar consumidores y contratos antes de modificar descubrimiento.

**Aceptación.** El mismo recorrido de memoria con menos schema anunciado, medición
de bytes/tokens y latencia, y pruebas de compatibilidad de clientes avanzados.
Si no mejora una métrica concreta, no agregar esa configuración.

## Límites de seguridad revisados

Se aplicaron `audit-method` y `audit-hard-stops` como revisión acotada, no como
certificación de los 13 dominios. La inspección de navegador siguió selección de
superficie y verificación visual de Computer Use, sobre una instancia sintética.

- H1/H2/H11: no hay base Supabase ni bundle SPA con service-role; aislamiento MCP
  ejercitado. No se afirmó un escaneo completo de todas las formas de secretos.
- H3: gates HTTP centrales inspeccionados; MCP anónimo rechazado en vivo y tests
  de viewer/operator, scope y tenant aprobados. Stdio mantiene su contexto local.
- H4: búsqueda de nombres rastreados encontró `.env.example`, no un `.env` real;
  no se ejecutó scanner de secretos ni historia Git. Esto queda sin certificar.
- H5: no hay receptor de webhook público en las rutas revisadas; captura por hook
  local no se evalúa como webhook firmado de terceros.
- H6/H7: escape de texto del dashboard y bindings SQL del selector/ledger inspeccionados
  puntualmente. No es una revisión exhaustiva de cada sink XSS/SQL del repositorio.
- H8: sin bypass de auth demostrado en los caminos ejercitados; Origin débil y
  legacy explícito documentados en E2E-04.
- H9: el demo usa SQLite real desechable y valida consecuencias de retiro;
  faltaba la segunda ejecución de consolidación, ahora reproducida por un contrato rojo.
- H10: MCP tiene bearer y Dreaming tiene límites de llamadas/tokens; no se verificó
  un endpoint costoso anónimo. Cuotas del proveedor y costos monetarios no auditados.

## Qué no cambiaría y qué falta verificar

Conservar SQLite como autoridad, rehidratación autorizada, evidence lineage,
candidate-first, filtros de sensibilidad, retiros, manifiestos exactos y activaciones
separadas. No reducir los gates de graph para producir observaciones artificialmente.
No adoptar código de los clones de investigación como parte de esta revisión.

Quedan fuera de esta verificación: suite completa/ML, carga sostenida, UI móvil,
todos los clientes/SDK, Postgres en producción, matriz CI, envío real de compact a
un agente, precisión humana y restore de un backup/NAS productivo reciente. El backup
NAS tiene último código programado 0; eso no demuestra restaurabilidad actual.

Las conexiones directas a bases existentes usaron `mode=ro` y `query_only`.
No se cambiaron memorias existentes, jobs, features, credenciales, tareas ni servicios.
La excepción explícita es el registro de un nuevo hallazgo como claim candidato
148267 (`mm-ed04~5`), requerido por AGENTS; no fue promovido por esta revisión.
MCP query se usó aparte y no se certifica ausencia de contabilidad interna de acceso.

El servidor/browser temporal se cerró y se comprobó que su listener desapareció.
Se limpiaron dos directorios propios de fixtures tras liberar locks de SQLite en
Windows. Una prueba inicial eligió otra interfaz de red; la comprobación corregida
usó el host registrado y descartó una falsa caída. Una invocación de tests tenía
dos nombres inexistentes y se reemplazó por los archivos reales; las cifras de
pruebas anteriores corresponden solo a las ejecuciones completas.

## Comandos para reproducir y continuar

Desde la raíz del checkout:

```powershell
python -m pytest artifacts/e2e-review-20260907/test_review_contracts.py -q --tb=short
python artifacts/e2e-review-20260907/probe-recall.py
python artifacts/e2e-review-20260907/probe-csrf.py
python -m pytest tests/test_public_demo.py tests/test_public_v1.py tests/test_dreaming_worker.py -q
python -m ruff check artifacts/e2e-review-20260907
```

El primer comando debe dar **cuatro fallos en el código revisado**; no es un gate
verde ni un parche. Tras autorizar correcciones, promover los contratos adecuados
a tests, agregar casos adversariales y verificar con el alcance de desarrollo del repo.
Implementar E2E-01, E2E-02 y E2E-03 en cambios separados; recuperación de datos y
despliegue permanecen decisiones posteriores y explícitas.
