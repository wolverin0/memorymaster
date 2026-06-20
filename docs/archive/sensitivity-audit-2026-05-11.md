# Sensitivity-Filter Leak Audit - 2026-05-11

## Methodology

- Branch: `chore/sensitivity-audit-2026-05-11`, created from `origin/main`.
- Database: `memorymaster.db`, opened read-only through Python's `sqlite3` module.
- Table/column scanned: `claims.text`.
- Row count from `SELECT COUNT(*) FROM claims`: 39,176 claims.
- Non-empty text row count from `SELECT COUNT(*) FROM claims WHERE text IS NOT NULL AND text != ""`: 39,176 claims.
- Pattern counts used `SELECT COUNT(*) FROM claims WHERE REGEXP_NAME(?, text)` with a Python-backed SQLite regexp function.
- Sample IDs used `SELECT id, text FROM claims WHERE REGEXP_NAME(?, text) ORDER BY id LIMIT 5`.
- Sample snippets below show the first 100 characters of `claim.text`, with any matched sensitive substrings masked in this report so the audit does not create a second committed leak.

Canonical source note: `memorymaster/redact.py` is not present on this checkout. The active redaction implementation is `memorymaster/security.py`. The audit used the seven requested expressions, cross-checked against `security.py` where applicable. `security.py` intentionally does not redact bare private IPv4 addresses at ingest time; it only redacts `private_ip_port` and handles bare private IPs in export-time dream filtering.

## Hit Counts

| Pattern | Regex | Flags | Hits |
|---|---|---:|---:|
| `api_key` | `(api_key\|apikey)\s*[:=]\s*\S+` | case-insensitive | 23 |
| `bearer_token` | `(bearer\s+[A-Za-z0-9\-._~+/]+=*)` | case-insensitive | 56 |
| `jwt` | `(eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]*)` | case-sensitive | 0 |
| `aws_key` | `((AKIA\|ASIA\|AROA\|AIDA)[A-Z0-9]{16})` | case-sensitive | 0 |
| `ssh_private_key` | `(-----BEGIN [A-Z]+ PRIVATE KEY-----)` | case-sensitive | 0 |
| `password_assignment` | `(password\|passwd\|pwd)\s*[:=]\s*\S+` | case-insensitive | 11 |
| `private_ip` | `(10\|172\.(?:1[6-9]\|2\d\|3[01])\|192\.168)\.\d{1,3}\.\d{1,3}` | case-sensitive | 284 |

## Samples

### `api_key` - 23 hits

| Claim ID | First 100 characters of `claim.text` |
|---:|---|
| 752 | `UNMS/UISP Backup Fix (2026-03-02)\n- **Problema:** cron "Nightly Backup Trigger" usaba `$N8N_FEEDS_A` |
| 808 | `UNMS/UISP Backup Fix (2026-03-02)\n- **Problema:** cron "Nightly Backup Trigger" usaba `$N8N_FEEDS_A` |
| 980 | `## Environment Variables (.env.example)  ``` SECRET_KEY=<flask-secret> DEBUG=True GOOGLE_[REDACTED:api_key]` |
| 1027 | `## AI Processing (choose one or both for redundancy)  OPENAI_[REDACTED:api_key] GEMINI_API` |
| 1104 | `## Environment Variables  ``` VITE_SUPABASE_URL= VITE_SUPABASE_ANON_KEY= VITE_CLERK_PUBLISHABLE_KEY=` |

### `bearer_token` - 56 hits

| Claim ID | First 100 characters of `claim.text` |
|---:|---|
| 141 | `Auto-recall configurado para MemoryKing shared brain. Dos hooks en Claude Code: (1) SessionStart - b` |
| 351 | `[Claude Memory: openclaw-debug, Project: openclaw2claude]  # OpenClaw Debug Notes  ## Installation L` |
| 815 | `Infrastructure credentials reference (no passwords stored here): - ClawTrol DB: PGPASSWORD in ~/.ope` |
| 902 | `🌐 Conectividad: Acceso Externo a Archivos (Update 13:02)\n- **Hito**: Se habilitó el acceso externo ` |
| 7725 | `# Memory — 2026-03-02  ## eye2byte: Setup canónico (Windows-native) - Corre como Python 3.12 nativo ` |

### `jwt` - 0 hits

No matching rows.

### `aws_key` - 0 hits

No matching rows.

### `ssh_private_key` - 0 hits

No matching rows.

### `password_assignment` - 11 hits

| Claim ID | First 100 characters of `claim.text` |
|---:|---|
| 980 | `## Environment Variables (.env.example)  ``` SECRET_KEY=<flask-secret> DEBUG=True GOOGLE_[REDACTED:api_key]` |
| 2070 | `## Pattern 4: Password Setup After Invite  **What:** Dedicated page where invited users set their pa` |
| 3921 | `## Should show: DB_[REDACTED:password_assignment]` |
| 3928 | `## Common Issues & Fixes  | Symptom | Cause | Fix | |---------|-------|-----| | All APIs return 500 ` |
| 3930 | `## Database (MUST match PostgreSQL container)  DB_[REDACTED:password_assignment] DATABASE_URL=postgresql://das` |

### `private_ip` - 284 hits

| Claim ID | First 100 characters of `claim.text` |
|---:|---|
| 53 | `WhatsApp Bot deployment procedure: Dashboard code IS volume-mounted → scp + docker restart. Bot dirs` |
| 58 | `Infraestructura\n- Jellyfin: http://[REDACTED:private_ip]:8096\n- qBittorrent: http://[REDACTED:private_ip]:8080` |
| 77 | `[Claude Memory: clawtrol-api, Project: pauol]  # ClawTrol API Reference (for CRM/OpenClaw work)  ## ` |
| 79 | `User Messages (Snake)\n- **[00:04]** 582bse creo hoy hace un raro no s qué boludes decís\n- **[00:06` |
| 161 | `Android Node — S25 Ultra - APK compilado desde repo oficial openclaw (2026.3.14-dev) en windows-wsl ` |

## Recommended Remediation

Do not bulk-redact blindly from the sample list alone. Re-run the same read-only selection to build complete ID sets for each pattern, then review by pattern class.

Recommended order:

1. Redact `api_key`, `bearer_token`, and `password_assignment` hits first. These are direct credential-like leaks or high-risk authentication strings.
2. Confirm whether `private_ip` should be treated as an ingest-time leak. The current `security.py` comment says bare private IPs are intentionally allowed in claims, while `private_ip_port` is redacted. If policy changes, redact the matching claims or migrate them to lower-precision topology descriptions.
3. Leave `jwt`, `aws_key`, and `ssh_private_key` as no-op categories for this audit because they returned zero hits.

Manual remediation path for a reviewed claim:

```powershell
python -m memorymaster --db memorymaster.db redact-claim <claim_id> --mode redact --claims-only --reason "sensitivity audit 2026-05-11" --actor "sensitivity-audit"
```

Bulk remediation path:

- Write a short script that opens `memorymaster.db`, registers the same regexp names, selects matching claim IDs, and calls `MemoryService.redact_claim_payload(claim_id, mode="redact", redact_claim=True, redact_citations=False, reason="sensitivity audit 2026-05-11", actor="sensitivity-audit")`.
- Dry-run the script first and emit only claim IDs/counts.
- Keep a separate review allowlist for likely false positives, especially `bearer_token` prose matches and `private_ip` infrastructure claims.
- After remediation, re-run this exact audit and confirm all credential-class counts are zero.

## Final Checks

- This audit did not modify claim text, citations, events, or any SQLite table.
- This report is the only intended repository file change.
- GitHub origin substring check: `wolverin0/memorymaster` uses `wolverin0`, not `wolverinaton`.
