# OmniClaude Monitoring Setup Prompt

> Copy-paste this prompt into each project session. It runs graphify, leverages
> existing intelligence layers, and produces a monitoring.md for the OmniClaude
> orchestrator. The prompt is designed to minimize token waste by using cached
> knowledge before cold exploration.

---

## The Prompt

```
Vamos a preparar este proyecto para ser monitoreado por OmniClaude — un orchestrator
que vigila todos los proyectos via la tool Monitor de Claude Code y reacciona a errores
en tiempo real.

### PASO 0 — Corré graphify PRIMERO (si no existe graphify-out/)

Si el directorio graphify-out/ NO existe en este proyecto:

1. Corré `/graphify .` para construir el knowledge graph
2. Después corré `graphify claude install` para el always-on hook
3. Esperá a que termine antes de continuar

Si graphify-out/ YA existe, saltá este paso.

### PASO 1 — Leé las capas de inteligencia que ya tenemos ANTES de explorar

NO hagas un cold-explore del codebase entero. Primero leé estas fuentes (en orden de prioridad):

1. **graphify-out/GRAPH_REPORT.md** — god nodes, communities, surprising connections (si existe)
2. **CLAUDE.md** y **AGENTS.md** — ya están en tu contexto, usá la info de ahí
3. **GitNexus** — si hay `.gitnexus/` en el proyecto, usá `gitnexus_query({query: "architecture"})` para obtener el overview sin leer archivos raw
4. **MemoryMaster** — corré `mcp__memorymaster__query_memory` con el nombre del proyecto para ver qué claims ya tenemos
5. **obsidian-vault/wiki/** — si hay artículos para este proyecto, leelos antes de explorar
6. **README.md**, **package.json** o **pyproject.toml**, **git log --oneline -10** — solo lo mínimo que las fuentes anteriores no cubran

Solo después de agotar estas fuentes, explorá archivos que ninguna fuente cubrió.

### PASO 2 — Creá monitoring.md

Creá un archivo `monitoring.md` en la raíz del proyecto con esta estructura EXACTA.
OmniClaude va a parsear este archivo — respetá los headers y el frontmatter al pie de la letra.

```yaml
---
project: <nombre del proyecto (usar el nombre del directorio o git remote)>
path: <path completo absoluto del proyecto, ej: G:\_OneDrive\OneDrive\Desktop\Py Apps\miproyecto>
stack: <lenguaje/framework principal, ej: "Python 3.12 + FastAPI + SQLite">
repo: <URL del repo git si existe, o "local-only">
entry_point: <comando para levantar el proyecto, ej: "python -m uvicorn app:main --port 8000">
test_command: <comando para correr tests, o "none">
build_command: <comando para build/compile, o "none">
health_check: <URL para verificar que anda, o "none">
mcp_servers: <MCPs que usa este proyecto, ej: "memorymaster, gitnexus">
has_graphify: <true/false — si graphify-out/ existe>
has_gitnexus: <true/false — si .gitnexus/ existe>
has_memorymaster: <true/false — si hay claims en scope project:<nombre>>
---
```

Después del frontmatter, completá estas secciones:

```markdown
## What This Project Does
(2-3 oraciones. Qué hace, para quién, por qué existe.)

## Architecture Summary
(Si graphify-out/GRAPH_REPORT.md existe, citá los god nodes y communities principales.
Si GitNexus existe, citá los clusters funcionales. Si ninguno existe, describí la
arquitectura en 5-10 líneas basándote en la estructura de archivos.)

## Current State
(Últimos 5-10 commits del git log. Qué se estuvo haciendo. Qué funciona, qué está roto.
Si hay claims en MemoryMaster, citá los 3 más recientes.)

## Key Files
(Los 5-10 archivos más importantes. Si graphify reportó god nodes, usá esos. Formato:)
- `path/to/file.py` — qué hace (1 línea)

## Active Issues
(Bugs conocidos, features pendientes, blockers. Si hay GitHub issues, listalos.
Si MemoryMaster tiene claims tipo bug/gotcha para este proyecto, incluilos.)

## Monitoring Signals

### Logs to Watch
(Paths EXACTOS de archivos de log que OmniClaude debería monitorear con la tool Monitor.
Para cada uno: path, qué buscar, qué significa. Formato:)
- **path**: `path/to/app.log`
  **pattern**: `ERROR|CRITICAL|Traceback`
  **meaning**: error de runtime en la app principal
  **action**: leer el traceback completo, identificar root cause, intentar fix

### Health Checks
(URLs o comandos que verifican que el servicio está up. Formato:)
- **check**: `curl -s http://localhost:8000/health`
  **expect**: HTTP 200 con `{"status": "ok"}`
  **frequency**: cada 5 minutos
  **on_failure**: reiniciar el servicio con `<comando>`

### Tests to Run
(El comando de test y qué indica un fallo. Formato:)
- **command**: `python -m pytest tests/ -q --tb=line`
  **success**: "X passed" sin failures
  **on_failure**: leer el output, identificar el test que falló, correr solo ese test con -v

### Build Signals
(Si el proyecto tiene build, qué indica un build roto. Formato:)
- **command**: `npm run build 2>&1`
  **success**: "Build complete" o exit code 0
  **on_failure**: leer errores de TypeScript/webpack, corregir

### File Watchers
(Archivos que si cambian indican que algo importante pasó. Formato:)
- **path**: `memorymaster.db`
  **trigger**: size decrease > 10%
  **meaning**: posible corrupción o truncamiento
  **action**: verificar integridad con `sqlite3 memorymaster.db "PRAGMA integrity_check"`

## How to Verify It Works
(Pasos concretos para que OmniClaude verifique que el proyecto está healthy sin
intervención humana. Cada paso debe ser un comando ejecutable.)

1. `<comando>` — qué verificar en el output
2. `<comando>` — qué verificar
3. ...

## Dependencies on Other Projects
(Si este proyecto depende de otros proyectos del workspace — listarlos con su path.
Ej: "Depende de memorymaster para MCP memory" con path G:\...\memorymaster)
```

### PASO 3 — Verificá el monitoring.md

Después de crearlo:
1. Verificá que el frontmatter es YAML válido
2. Verificá que todos los paths son absolutos y correctos
3. Verificá que los health checks realmente responden (corré el curl/comando)
4. Verificá que el test_command funciona
5. Si algo no existe o no sabés, poné "unknown" — NO inventes

### PASO 4 — Ingestá en MemoryMaster

Después de verificar, corré:
```
mcp__memorymaster__ingest_claim con:
  text: "monitoring.md creado para <proyecto> con <N> señales de monitoreo: <resumen>"
  claim_type: "architecture"
  subject: "<nombre del proyecto>"
  scope: "project:<nombre>"
  source_agent: "claude-session"
```

Esto permite que OmniClaude consulte MemoryMaster para saber qué proyectos
tienen monitoring configurado.
```

---

## Notas para OmniClaude (el orchestrator)

OmniClaude usa la tool **Monitor** de Claude Code para:
- Streamear stdout/stderr de procesos en background
- Detectar patrones en logs en tiempo real (regex sobre cada línea)
- Reaccionar automáticamente: leer contexto → diagnosticar → intentar fix

El flujo es:
1. OmniClaude lee `monitoring.md` de cada proyecto
2. Parsea las señales de monitoreo
3. Lanza Monitor sobre los logs/health checks definidos
4. Cuando detecta un patrón (ERROR, test failure, health check down):
   a. Lee el contexto completo (graphify-out/GRAPH_REPORT.md + MemoryMaster claims)
   b. Diagnostica la causa probable
   c. Intenta un fix automatico o escala al usuario via Telegram
5. Ingest el incidente como claim en MemoryMaster para que quede registrado
