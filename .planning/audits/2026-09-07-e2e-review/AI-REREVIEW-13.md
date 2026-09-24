<!-- doc-head: AI re-review rejects all 13 emitted statements as standalone current memory -->
Revisado 2026-09-08: evidencia, vigencia, duplicados, utilidad marginal y destino canonico.
Evaluacion de IA sobre el mismo lote congelado; no son etiquetas humanas ni modifica claims.
La documentacion local se contrasto hoy; no hubo pruebas en hosts ni validacion de produccion.
<!-- /doc-head -->

# Reevaluacion de los 13 registros

## Dictamen

0/13 aceptados tal como fueron emitidos. Esto NO significa que los 13 sean falsos:
varios son ciertos pero redundantes, mal ubicados, incompletos o propios de documentos
operativos. La seleccion anterior confundio respaldo textual con utilidad marginal.
Los registros 3/12 y 4/7 son dos pares de duplicados semanticos.

| N | Registro | Destino / decision | Fundamento y contraste |
|---|---|---|---|
| 1 | Delegacion total de ingenieria | reject | Una autorizacion contextual resumida por IA no debe convertirse en permiso permanente. La fuente es un resumen generado y no una autorizacion actual delimitada. Fuente: `original cited summary`. |
| 2 | Alquileres compartia DonWeb/Caddy | history_only | La dependencia termino; conservar el incidente en el historial de infraestructura, no inyectar cronologia como memoria global. Fuente: `infra/docs/DOMAINS.md:185-218`. |
| 3 | Alquileres estrena tunel 28/08 | documentation | Topologia documentada en PLACEMENT y DOMAINS. La memoria separada no agrega informacion; coincide con el registro 12. Si se consulta, leer el mapa actual. Fuente: `infra/PLACEMENT.md:122; infra/docs/DOMAINS.md:18`. |
| 4 | Pedrito se muda a Vultr 01/09 | documentation | Hecho corroborado documentalmente, pero la topologia completa incluye standby frio en DonWeb. Una frase global y duplicada con 7 puede inducir a borrar o arrancar el standby. Fuente: `infra/SYSTEM.md:155; infra/PLACEMENT.md:128-133`. |
| 5 | Eve suspendido y reemplazado por roadmap | rewrite_before_use | La frase omite la excepcion posterior: pruebas aisladas del rethink autorizadas y jobs P0 historicos aun suspendidos. Consultar la autoridad especifica, no recordar una suspension global indiferenciada. Fuente: `_runtime/finalorchestra-mcp/docs/EVE-SUSPENSION.md:1-11; docs/EVE-ROADMAP.md:14-20`. |
| 6 | Se documento el riesgo de editar scripts activos | documentation | El hecho de haber documentado algo no es la leccion. La regla preventiva y sus procedimientos pertenecen al runbook y controles de ejecucion; ya estan en GUARDA-VOLUMENES y el mapa obliga a leerlo. Un indice breve solo aporta si falta descubribilidad. Fuente: `infra/docs/GUARDA-VOLUMENES.md:20-25; infra/docs/DOCS-MAP.md`. |
| 7 | Pedrito migro despues a Vultr | duplicate | Duplica el registro 4, pierde fecha y esta atribuido al transportista wezbridge. No aporta evidencia independiente de topologia actual. Fuente: `original A2A; infra/PLACEMENT.md:128-133`. |
| 8 | El drain de wezbridge pertenece a wezbridge | reject | Es metadato obvio de propiedad del codigo, recuperable del repositorio. El aprendizaje util seria el fallo de expiracion en drain y su regresion; esta frase no lo captura. Fuente: `original cited diagnostic`. |
| 9 | Vaultwarden ya implementado con DPAPI | documentation | La cita original solo anuncia el trabajo; no prueba implementacion. El runbook actual si documenta el mecanismo. Usar ese procedimiento vigente, sin almacenar detalles de acceso como otra verdad paralela. Fuente: `infra/docs/VAULTWARDEN-WRITE-METHOD.md:14-16,108-109,131-132`. |
| 10 | sudo sin password para usuario en Omarchy | defer_verification | La cita dice sudo -n, pero no identifica el usuario ni permisos exactos. No se verifico el host y la busqueda acotada en HOSTS no corrobora esa asociacion. Un inventario de acceso debe ser autoridad; no inferir permiso desde memoria. Fuente: `original cited message; bounded search infra/docs/HOSTS.md`. |
| 11 | Jellyfin instalado y ruta de biblioteca | documentation | PLACEMENT corrobora version y ruta, pero la cita original no respalda todos los detalles. Es inventario operativo mutable, ya mantenido en su documento; no memoria independiente. Fuente: `infra/PLACEMENT.md:54`. |
| 12 | Alquileres tiene tunel desde 28/08 | duplicate | Mismo hecho que 3, reenviado por A2A. Reforzarlo no agrega una fuente independiente. La respuesta original admite que no inspecciono topologia interna. Fuente: `original A2A; infra/PLACEMENT.md:122; infra/docs/DOMAINS.md:18`. |
| 13 | Firefly unavailable sin inventar saldo | rule_and_status_separate | El 401 es un estado transitorio que debe comprobar el conector. No inventar saldo es un contrato de producto para codigo/tests. El plan actual ya registra ambos y exige no representar ausencia como cero; no hace falta otra memoria global. Fuente: `personaldashboard/docs/OMNIGOD-REMEDIATION-PLAN.md:53-55; src/components/widgets/FireflyWidget.tsx:76-130`. |

## Criterio corregido

- Memoria: aprendizaje no obvio, duradero, con consecuencia concreta en futuras decisiones; evidencia suficiente, alcance correcto y sin duplicar autoridad canonica.
- AGENTS.md: reglas estables sobre como trabajar y donde consultar; no un inventario de servidores ni un diario de incidentes.
- Runbook / inventario: topologia, accesos, ubicaciones, procedimientos y estado mantenido por el proyecto.
- Codigo / test: comportamiento obligatorio del producto, como distinguir saldo ausente de cero.
- Historial: cronologia de migraciones e incidentes cerrados que no necesita entrar en el contexto normal.
- Una memoria breve que apunta al documento puede ser util si evita un error recurrente y el mapa no basta. La duplicacion no se justifica solo porque el dato sea verdadero.

## Responsabilidad y limites

El agente revisa evidencia, contradicciones, duplicacion y utilidad tecnica antes de pedir
revision al operador. Lo desconocido se marca como tal y se verifica en la fuente adecuada;
no se traslada al operador una pregunta que el agente puede resolver leyendo codigo/docs.
Solo preferencias personales o juicios de negocio no inferibles requieren su aporte.

AI-REREVIEW-13.jsonl es una nueva fixture de evaluacion reutilizable; conserva IDs,
fingerprint y resultados observados. Usa label_origin=ai y human_accept=null.
current_validity evalua el contenido frente a la documentacion disponible: true no
es prueba de runtime. Se deja null cuando no puede resolverse; verification_status
distingue documentado, historico, incompleto y desconocido. Rechazar por falta de
utilidad NO convierte automaticamente un hecho verdadero en falso.
La revision anterior y su cohorte permanecen intactas para poder comparar.

El evaluador existente proceso los 13: useful_precision=0, 13 emisiones no deseadas.
Es un diagnostico local de este lote y este criterio corregido, no una estimacion del
sistema entero. Sus otras metricas (por ejemplo citas exactas) no prueban vigencia ni
utilidad. No se corrigio el extractor, deduplicador, ranking ni el contenido del almacen.
No se promueve, elimina ni mueve ninguna memoria con este informe.
