# Sensitivity Filter Rules

MemoryMaster ingests ambient data from LLM transcripts, dream bridge, and MCP calls. The sensitivity filter is the firewall that prevents secrets from being stored. Bypassing it — accidentally or "just this once" — is a security incident.

## Never ingest

- API keys, tokens, bearer headers, auth cookies — regardless of provider
- Passwords, PINs, OTP codes
- Personal home directory paths that expose usernames
- Raw code snippets longer than ~3 lines (use symbol references instead)
- Database connection strings with credentials
- Email addresses of third parties (user's own email is OK if explicitly opted in)
- Payment card data, SSN, national ID numbers

## IP privadas: dos escritores, dos políticas (a propósito)

- `redact_text` / `_redact` (fuentes crudas ADR-0006, evidencia, prefiltro de export) **no** redacta una IP
  privada suelta: una claim de infraestructura sin la IP es inútil. Fijado por
  `tests/test_security_patterns.py::TestPrivateIPv4NotRedactedAtIngest`.
- `sanitize_claim_input` (todo escritor de claims) **sí** la redacta (`_CLAIM_ONLY_PATTERNS`, `private_ipv4`,
  más rutas absolutas): la memoria de claims tiene un contrato más estricto. Fijado por
  `tests/test_claim_intake_private_context.py` y `test_private_ip_policy_differs_by_writer_on_purpose`.
- La IP + puerto en prosa se redacta en ambos (`private_ip_port`); `dream_bridge.py` y `profile/egress.py`
  redactan al exportar, incluida la IP suelta.

Una versión anterior de esta regla decía "las IP privadas se ingieren" sin distinguir escritor y contradecía al
código de claims. Si se cambia una política, el cambio va en `security.py`, en su test y aquí, juntos.

Formas que el filtro cubre además de las claves de proveedor: contraseña en prosa en español
(`la contraseña ... es X`, `clave: X`; el valor lleva un símbolo `!@#$%^&*+~?`, o mayúscula+minúscula+2 dígitos, o va entre comillas con letra y dígito; `clave primaria`, el verbo `pass` y valores tipo `utf8mb4`/`admin1234` pasan),
`esquema://usuario:clave@host` de cualquier esquema (sólo se redacta la clave), AWS secret access key con
contexto AWS, OTP/PIN con palabra clave, CUIT/CUIL con dígito verificador válido, rutas home con `/` o `\`.
Fuera de alcance: secretos partidos por espacios o saltos de línea, o en hex puro; palabra clave seguida de 9+ espacios o salto de línea antes del valor; `pin 8080` (puerto) y ids de 11 dígitos con dígito CUIT válido; contraseñas en minúsculas+dígitos sin símbolo ni comillas (`admin1234`) o con más de 256 caracteres; `/Users/Shared`, `C:/Users/Public` no se tratan como usuarios. Una huella SSH
`SHA256:<43 b64>` es pública y no se redacta.

Saldos transaccionales de clientes (T-2112): no los detiene este filtro (no son un secreto, no se redactan ni se rechazan en el ingest); `core/promotion_policy.states_customer_balance` impide que se confirmen solos y `atlas_llm_extractor` ni siquiera los ingiere. Cubre término de saldo/deuda + monto con moneda (o cero explícito) + cliente identificado (id `cliente 9001` o sujeto con nombre de persona) en una misma ventana; no cubre saldos en prosa sin id ni nombre, nombres sueltos en minúscula, ni montos sin símbolo de moneda. Los claims ya confirmados no se tocan.

## Where the filter applies

The filter MUST run on:
- `mcp_server.py:ingest_claim` — every MCP call from any client
- `dream_bridge.py` — every auto-ingest from Claude Auto Dream
- `service.py:ingest` — every direct service call (if callers bypass MCP)
- Any new ingest path — default-deny until filter is wired in

## Never add a bypass

There is no legitimate reason to have an `allow_sensitive=True` flag on ingest. If a genuine use case arises (e.g., structured secrets metadata for lifecycle management):
1. Add a separate typed table with its own access control, NOT a flag on claims.
2. Require explicit CLI argument, never default-on.
3. Document in SECURITY section of README.

## When you find a sensitive claim in the DB

1. Do NOT paste it into a log, a commit message, or a reply to the user.
2. Use `redact_claim_payload` to replace the text with a hash + redacted marker.
3. File an internal note about the ingest path that let it through.
4. Patch the filter pattern if new.

## Testing the filter

Every change to the filter must ship with a `tests/test_sensitivity_filter.py` case that proves the new pattern is caught. Red-bar first, green-bar second. If `pytest -k sensitivity` doesn't exist or is skipped, that's a bug.

## Filter scope is INGEST only — not display

Display-time redaction is a separate layer (`redact_claim_payload`, dashboard masks). The INGEST filter is the last line of defense; do not weaken it assuming display will catch it.
