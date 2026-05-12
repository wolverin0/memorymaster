# SQLite <-> Postgres Schema Parity Divergence

Audit scope: `memorymaster/schema.sql` and `memorymaster/schema_postgres.sql` only. This is documentation-only; no schema or code changes were made.

## Summary

- Tables in both schemas: `claims`, `citations`, `events`, `claim_embeddings`, `external_sources`, `source_items`, `evidence_items`, `action_proposals`, `mcp_usage`, `media_retry_queue`.
- Postgres-only table: `claim_links` (`memorymaster/schema_postgres.sql:272-280`).
- SQLite-only tables: none in `memorymaster/schema.sql` (`memorymaster/schema.sql:1-239`).
- FTS5 / tsvector: neither file defines SQLite FTS5 virtual tables nor a Postgres `tsvector`/GIN text-search equivalent (`memorymaster/schema.sql:1-239`, `memorymaster/schema_postgres.sql:1-314`).
- Highest-impact drift: `claims` column set differs, `claim_embeddings` storage differs by backend, Postgres has `claim_links`, and Postgres has sensitivity indexes absent from the static SQLite schema.

## Tables In Common

Legend: **MISMATCH** marks type, constraint, or column-existence drift visible in the schema files.

### `claims`

SQLite table: `memorymaster/schema.sql:3-35`. Postgres table and compatibility alters: `memorymaster/schema_postgres.sql:10-41`.

| Column | SQLite | Postgres | Alignment |
|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` (`memorymaster/schema.sql:4`) | `BIGSERIAL PRIMARY KEY` (`memorymaster/schema_postgres.sql:11`) | **MISMATCH**: integer width/sequence implementation differs by backend. |
| `text` | `TEXT NOT NULL` (`memorymaster/schema.sql:5`) | `TEXT NOT NULL` (`memorymaster/schema_postgres.sql:12`) | Aligned. |
| `idempotency_key` | `TEXT` (`memorymaster/schema.sql:6`) | `TEXT`, plus `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (`memorymaster/schema_postgres.sql:13`, `memorymaster/schema_postgres.sql:37-38`) | Aligned, with Postgres compatibility alter. |
| `normalized_text` | `TEXT` (`memorymaster/schema.sql:7`) | `TEXT` (`memorymaster/schema_postgres.sql:14`) | Aligned. |
| `claim_type` | `TEXT` (`memorymaster/schema.sql:8`) | `TEXT` (`memorymaster/schema_postgres.sql:15`) | Aligned. |
| `subject` | `TEXT` (`memorymaster/schema.sql:9`) | `TEXT` (`memorymaster/schema_postgres.sql:16`) | Aligned. |
| `predicate` | `TEXT` (`memorymaster/schema.sql:10`) | `TEXT` (`memorymaster/schema_postgres.sql:17`) | Aligned. |
| `object_value` | `TEXT` (`memorymaster/schema.sql:11`) | `TEXT` (`memorymaster/schema_postgres.sql:18`) | Aligned. |
| `scope` | `TEXT NOT NULL DEFAULT 'project'` (`memorymaster/schema.sql:12`) | `TEXT NOT NULL DEFAULT 'project'` (`memorymaster/schema_postgres.sql:19`) | Aligned. |
| `volatility` | `TEXT NOT NULL DEFAULT 'medium'` (`memorymaster/schema.sql:13`) | `TEXT NOT NULL DEFAULT 'medium'` (`memorymaster/schema_postgres.sql:20`) | Aligned. |
| `status` | `TEXT NOT NULL CHECK (...)` (`memorymaster/schema.sql:14-16`) | `TEXT NOT NULL CHECK (...)` (`memorymaster/schema_postgres.sql:21-23`) | Aligned. |
| `confidence` | `REAL NOT NULL DEFAULT 0.5 CHECK (...)` (`memorymaster/schema.sql:17`) | `DOUBLE PRECISION NOT NULL DEFAULT 0.5 CHECK (...)` (`memorymaster/schema_postgres.sql:24`) | **MISMATCH**: numeric type differs, constraint aligned. |
| `pinned` | `INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1))` (`memorymaster/schema.sql:18`) | `BOOLEAN NOT NULL DEFAULT FALSE` (`memorymaster/schema_postgres.sql:25`) | **MISMATCH**: SQLite uses integer boolean with CHECK; Postgres uses native boolean and lacks the explicit CHECK. Likely intentional backend type mapping. |
| `supersedes_claim_id` | `INTEGER` plus table FK (`memorymaster/schema.sql:19`, `memorymaster/schema.sql:33`) | `BIGINT REFERENCES claims(id) ON DELETE SET NULL` (`memorymaster/schema_postgres.sql:26`) | **MISMATCH**: integer width and inline-vs-table FK syntax differ; referential action aligned. |
| `replaced_by_claim_id` | `INTEGER` plus table FK (`memorymaster/schema.sql:20`, `memorymaster/schema.sql:34`) | `BIGINT REFERENCES claims(id) ON DELETE SET NULL` (`memorymaster/schema_postgres.sql:27`) | **MISMATCH**: integer width and inline-vs-table FK syntax differ; referential action aligned. |
| `created_at` | `TEXT NOT NULL` (`memorymaster/schema.sql:21`) | `TIMESTAMPTZ NOT NULL` (`memorymaster/schema_postgres.sql:28`) | **MISMATCH**: SQLite stores timestamps as text; Postgres uses timestamp with time zone. |
| `updated_at` | `TEXT NOT NULL` (`memorymaster/schema.sql:22`) | `TIMESTAMPTZ NOT NULL` (`memorymaster/schema_postgres.sql:29`) | **MISMATCH**: timestamp representation differs. |
| `last_validated_at` | `TEXT` (`memorymaster/schema.sql:23`) | `TIMESTAMPTZ` (`memorymaster/schema_postgres.sql:30`) | **MISMATCH**: timestamp representation differs. |
| `archived_at` | `TEXT` (`memorymaster/schema.sql:24`) | `TIMESTAMPTZ` (`memorymaster/schema_postgres.sql:31`) | **MISMATCH**: timestamp representation differs. |
| `human_id` | `TEXT` (`memorymaster/schema.sql:25`) | `TEXT` (`memorymaster/schema_postgres.sql:32`) | Aligned. |
| `tier` | `TEXT NOT NULL DEFAULT 'working'` (`memorymaster/schema.sql:26`) | Absent from Postgres `claims` (`memorymaster/schema_postgres.sql:10-35`) | **MISMATCH**: genuine column drift unless Postgres intentionally omits lifecycle tiering. |
| `access_count` | `INTEGER NOT NULL DEFAULT 0` (`memorymaster/schema.sql:27`) | Absent from Postgres `claims` (`memorymaster/schema_postgres.sql:10-35`) | **MISMATCH**: genuine column drift for access tracking. |
| `last_accessed` | `TEXT` (`memorymaster/schema.sql:28`) | Absent from Postgres `claims` (`memorymaster/schema_postgres.sql:10-35`) | **MISMATCH**: genuine column drift for access tracking. |
| `event_time` | `TEXT` (`memorymaster/schema.sql:29`) | Absent from Postgres `claims` (`memorymaster/schema_postgres.sql:10-35`) | **MISMATCH**: genuine column drift for bi-temporal ingestion. |
| `valid_from` | `TEXT` (`memorymaster/schema.sql:30`) | Absent from Postgres `claims` (`memorymaster/schema_postgres.sql:10-35`) | **MISMATCH**: genuine column drift for bi-temporal validity. |
| `valid_until` | `TEXT` (`memorymaster/schema.sql:31`) | Absent from Postgres `claims` (`memorymaster/schema_postgres.sql:10-35`) | **MISMATCH**: genuine column drift for bi-temporal validity. |
| `wiki_article` | `TEXT` (`memorymaster/schema.sql:32`) | `TEXT`, plus `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (`memorymaster/schema_postgres.sql:34`, `memorymaster/schema_postgres.sql:40-41`) | Aligned, with Postgres compatibility alter. |
| `tenant_id` | Absent from SQLite `claims` (`memorymaster/schema.sql:3-35`) | `TEXT` (`memorymaster/schema_postgres.sql:33`) | **MISMATCH**: Postgres-only tenancy column. Decide whether tenancy should be cross-backend or documented as Postgres-only. |

### `citations`

SQLite table: `memorymaster/schema.sql:72-80`. Postgres table: `memorymaster/schema_postgres.sql:83-90`.

| Column | SQLite | Postgres | Alignment |
|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` (`memorymaster/schema.sql:73`) | `BIGSERIAL PRIMARY KEY` (`memorymaster/schema_postgres.sql:84`) | **MISMATCH**: integer width/sequence implementation differs. |
| `claim_id` | `INTEGER NOT NULL` plus table FK cascade (`memorymaster/schema.sql:74`, `memorymaster/schema.sql:79`) | `BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE` (`memorymaster/schema_postgres.sql:85`) | **MISMATCH**: integer width and FK syntax differ; referential action aligned. |
| `source` | `TEXT NOT NULL` (`memorymaster/schema.sql:75`) | `TEXT NOT NULL` (`memorymaster/schema_postgres.sql:86`) | Aligned. |
| `locator` | `TEXT` (`memorymaster/schema.sql:76`) | `TEXT` (`memorymaster/schema_postgres.sql:87`) | Aligned. |
| `excerpt` | `TEXT` (`memorymaster/schema.sql:77`) | `TEXT` (`memorymaster/schema_postgres.sql:88`) | Aligned. |
| `created_at` | `TEXT NOT NULL` (`memorymaster/schema.sql:78`) | `TIMESTAMPTZ NOT NULL` (`memorymaster/schema_postgres.sql:89`) | **MISMATCH**: timestamp representation differs. |

### `events`

SQLite table: `memorymaster/schema.sql:82-92`. Postgres table: `memorymaster/schema_postgres.sql:92-101`.

| Column | SQLite | Postgres | Alignment |
|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` (`memorymaster/schema.sql:83`) | `BIGSERIAL PRIMARY KEY` (`memorymaster/schema_postgres.sql:93`) | **MISMATCH**: integer width/sequence implementation differs. |
| `claim_id` | `INTEGER` plus table FK cascade (`memorymaster/schema.sql:84`, `memorymaster/schema.sql:91`) | `BIGINT REFERENCES claims(id) ON DELETE CASCADE` (`memorymaster/schema_postgres.sql:94`) | **MISMATCH**: integer width and FK syntax differ; referential action aligned. |
| `event_type` | `TEXT NOT NULL` (`memorymaster/schema.sql:85`) | `TEXT NOT NULL` (`memorymaster/schema_postgres.sql:95`) | Aligned. |
| `from_status` | `TEXT` (`memorymaster/schema.sql:86`) | `TEXT` (`memorymaster/schema_postgres.sql:96`) | Aligned. |
| `to_status` | `TEXT` (`memorymaster/schema.sql:87`) | `TEXT` (`memorymaster/schema_postgres.sql:97`) | Aligned. |
| `details` | `TEXT` (`memorymaster/schema.sql:88`) | `TEXT` (`memorymaster/schema_postgres.sql:98`) | Aligned. |
| `payload_json` | `TEXT` (`memorymaster/schema.sql:89`) | `JSONB` (`memorymaster/schema_postgres.sql:99`) | **MISMATCH**: JSON storage differs. Likely intentional backend-native type mapping. |
| `created_at` | `TEXT NOT NULL` (`memorymaster/schema.sql:90`) | `TIMESTAMPTZ NOT NULL` (`memorymaster/schema_postgres.sql:100`) | **MISMATCH**: timestamp representation differs. |

### `claim_embeddings`

SQLite table: `memorymaster/schema.sql:106-112`. Postgres conditional table: `memorymaster/schema_postgres.sql:286-314`.

| Column | SQLite | Postgres | Alignment |
|---|---|---|---|
| `claim_id` | `INTEGER PRIMARY KEY` plus table FK cascade (`memorymaster/schema.sql:107`, `memorymaster/schema.sql:111`) | `BIGINT PRIMARY KEY REFERENCES claims(id) ON DELETE CASCADE` in both branches (`memorymaster/schema_postgres.sql:290-291`, `memorymaster/schema_postgres.sql:303-304`) | **MISMATCH**: integer width and FK syntax differ; referential action aligned. |
| `model` | `TEXT NOT NULL` (`memorymaster/schema.sql:108`) | `TEXT NOT NULL` in both branches (`memorymaster/schema_postgres.sql:292`, `memorymaster/schema_postgres.sql:305`) | Aligned. |
| `embedding_json` | `TEXT NOT NULL` (`memorymaster/schema.sql:109`) | Fallback branch only: `embedding_json TEXT NOT NULL` (`memorymaster/schema_postgres.sql:303-307`) | **MISMATCH**: absent when Postgres `vector` extension exists. |
| `embedding` | Absent from SQLite (`memorymaster/schema.sql:106-112`) | Vector branch only: `embedding VECTOR(1536) NOT NULL` (`memorymaster/schema_postgres.sql:290-294`) | **MISMATCH**: Postgres-only vector storage. Intentional when `vector` extension exists. |
| `updated_at` | `TEXT NOT NULL` (`memorymaster/schema.sql:110`) | `TIMESTAMPTZ NOT NULL` in both branches (`memorymaster/schema_postgres.sql:294`, `memorymaster/schema_postgres.sql:307`) | **MISMATCH**: timestamp representation differs. |

### `external_sources`

SQLite table: `memorymaster/schema.sql:114-122`. Postgres table: `memorymaster/schema_postgres.sql:155-163`.

| Column | SQLite | Postgres | Alignment |
|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` (`memorymaster/schema.sql:115`) | `BIGSERIAL PRIMARY KEY` (`memorymaster/schema_postgres.sql:156`) | **MISMATCH**: integer width/sequence implementation differs. |
| `source_type` | `TEXT NOT NULL` (`memorymaster/schema.sql:116`) | `TEXT NOT NULL` (`memorymaster/schema_postgres.sql:157`) | Aligned. |
| `display_name` | `TEXT NOT NULL` (`memorymaster/schema.sql:117`) | `TEXT NOT NULL` (`memorymaster/schema_postgres.sql:158`) | Aligned. |
| `config_json` | `TEXT` (`memorymaster/schema.sql:118`) | `JSONB` (`memorymaster/schema_postgres.sql:159`) | **MISMATCH**: JSON storage differs. Likely intentional backend-native type mapping. |
| `created_at` | `TEXT NOT NULL` (`memorymaster/schema.sql:119`) | `TIMESTAMPTZ NOT NULL` (`memorymaster/schema_postgres.sql:160`) | **MISMATCH**: timestamp representation differs. |
| `updated_at` | `TEXT NOT NULL` (`memorymaster/schema.sql:120`) | `TIMESTAMPTZ NOT NULL` (`memorymaster/schema_postgres.sql:161`) | **MISMATCH**: timestamp representation differs. |
| `UNIQUE (source_type, display_name)` | Present (`memorymaster/schema.sql:121`) | Present (`memorymaster/schema_postgres.sql:162`) | Aligned. |

### `source_items`

SQLite table: `memorymaster/schema.sql:124-141`. Postgres table and compatibility alter: `memorymaster/schema_postgres.sql:165-181`, `memorymaster/schema_postgres.sql:242-243`.

| Column | SQLite | Postgres | Alignment |
|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` (`memorymaster/schema.sql:125`) | `BIGSERIAL PRIMARY KEY` (`memorymaster/schema_postgres.sql:166`) | **MISMATCH**: integer width/sequence implementation differs. |
| `source_id` | `INTEGER NOT NULL` plus table FK cascade (`memorymaster/schema.sql:126`, `memorymaster/schema.sql:139`) | `BIGINT NOT NULL REFERENCES external_sources(id) ON DELETE CASCADE` (`memorymaster/schema_postgres.sql:167`) | **MISMATCH**: integer width and FK syntax differ; referential action aligned. |
| `source_item_id` | `TEXT NOT NULL` (`memorymaster/schema.sql:127`) | `TEXT NOT NULL` (`memorymaster/schema_postgres.sql:168`) | Aligned. |
| `item_type` | `TEXT NOT NULL` (`memorymaster/schema.sql:128`) | `TEXT NOT NULL` (`memorymaster/schema_postgres.sql:169`) | Aligned. |
| `chat_id` | `TEXT` (`memorymaster/schema.sql:129`) | `TEXT` (`memorymaster/schema_postgres.sql:170`) | Aligned. |
| `sender_id` | `TEXT` (`memorymaster/schema.sql:130`) | `TEXT` (`memorymaster/schema_postgres.sql:171`) | Aligned. |
| `sender_name` | `TEXT` (`memorymaster/schema.sql:131`) | `TEXT` (`memorymaster/schema_postgres.sql:172`) | Aligned. |
| `occurred_at` | `TEXT` (`memorymaster/schema.sql:132`) | `TIMESTAMPTZ` (`memorymaster/schema_postgres.sql:173`) | **MISMATCH**: timestamp representation differs. |
| `text` | `TEXT` (`memorymaster/schema.sql:133`) | `TEXT` (`memorymaster/schema_postgres.sql:174`) | Aligned. |
| `payload_json` | `TEXT` (`memorymaster/schema.sql:134`) | `JSONB` (`memorymaster/schema_postgres.sql:175`) | **MISMATCH**: JSON storage differs. Likely intentional backend-native type mapping. |
| `content_hash` | `TEXT` (`memorymaster/schema.sql:135`) | `TEXT` (`memorymaster/schema_postgres.sql:176`) | Aligned. |
| `sensitivity` | `TEXT CHECK (...)` (`memorymaster/schema.sql:136`) | `TEXT CHECK (...)`, plus `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (`memorymaster/schema_postgres.sql:177`, `memorymaster/schema_postgres.sql:242-243`) | Aligned, with Postgres compatibility alter. |
| `created_at` | `TEXT NOT NULL` (`memorymaster/schema.sql:137`) | `TIMESTAMPTZ NOT NULL` (`memorymaster/schema_postgres.sql:178`) | **MISMATCH**: timestamp representation differs. |
| `updated_at` | `TEXT NOT NULL` (`memorymaster/schema.sql:138`) | `TIMESTAMPTZ NOT NULL` (`memorymaster/schema_postgres.sql:179`) | **MISMATCH**: timestamp representation differs. |
| `UNIQUE (source_id, source_item_id)` | Present (`memorymaster/schema.sql:140`) | Present (`memorymaster/schema_postgres.sql:180`) | Aligned. |

### `evidence_items`

SQLite table: `memorymaster/schema.sql:143-155`. Postgres table and compatibility alter: `memorymaster/schema_postgres.sql:183-194`, `memorymaster/schema_postgres.sql:244-245`.

| Column | SQLite | Postgres | Alignment |
|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` (`memorymaster/schema.sql:144`) | `BIGSERIAL PRIMARY KEY` (`memorymaster/schema_postgres.sql:184`) | **MISMATCH**: integer width/sequence implementation differs. |
| `source_item_id` | `INTEGER NOT NULL` plus table FK cascade (`memorymaster/schema.sql:145`, `memorymaster/schema.sql:154`) | `BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE` (`memorymaster/schema_postgres.sql:185`) | **MISMATCH**: integer width and FK syntax differ; referential action aligned. |
| `evidence_type` | `TEXT NOT NULL` (`memorymaster/schema.sql:146`) | `TEXT NOT NULL` (`memorymaster/schema_postgres.sql:186`) | Aligned. |
| `text` | `TEXT` (`memorymaster/schema.sql:147`) | `TEXT` (`memorymaster/schema_postgres.sql:187`) | Aligned. |
| `media_path` | `TEXT` (`memorymaster/schema.sql:148`) | `TEXT` (`memorymaster/schema_postgres.sql:188`) | Aligned. |
| `provider` | `TEXT` (`memorymaster/schema.sql:149`) | `TEXT` (`memorymaster/schema_postgres.sql:189`) | Aligned. |
| `confidence` | `REAL CHECK (...)` (`memorymaster/schema.sql:150`) | `DOUBLE PRECISION CHECK (...)` (`memorymaster/schema_postgres.sql:190`) | **MISMATCH**: numeric type differs, constraint aligned. |
| `payload_json` | `TEXT` (`memorymaster/schema.sql:151`) | `JSONB` (`memorymaster/schema_postgres.sql:191`) | **MISMATCH**: JSON storage differs. Likely intentional backend-native type mapping. |
| `sensitivity` | `TEXT CHECK (...)` (`memorymaster/schema.sql:152`) | `TEXT CHECK (...)`, plus `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (`memorymaster/schema_postgres.sql:192`, `memorymaster/schema_postgres.sql:244-245`) | Aligned, with Postgres compatibility alter. |
| `created_at` | `TEXT NOT NULL` (`memorymaster/schema.sql:153`) | `TIMESTAMPTZ NOT NULL` (`memorymaster/schema_postgres.sql:193`) | **MISMATCH**: timestamp representation differs. |

### `action_proposals`

SQLite table: `memorymaster/schema.sql:157-179`. Postgres table: `memorymaster/schema_postgres.sql:196-215`.

| Column | SQLite | Postgres | Alignment |
|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` (`memorymaster/schema.sql:158`) | `BIGSERIAL PRIMARY KEY` (`memorymaster/schema_postgres.sql:197`) | **MISMATCH**: integer width/sequence implementation differs. |
| `proposal_type` | `TEXT NOT NULL` (`memorymaster/schema.sql:159`) | `TEXT NOT NULL` (`memorymaster/schema_postgres.sql:198`) | Aligned. |
| `title` | `TEXT NOT NULL` (`memorymaster/schema.sql:160`) | `TEXT NOT NULL` (`memorymaster/schema_postgres.sql:199`) | Aligned. |
| `description` | `TEXT` (`memorymaster/schema.sql:161`) | `TEXT` (`memorymaster/schema_postgres.sql:200`) | Aligned. |
| `source_item_id` | `INTEGER` plus table FK set null (`memorymaster/schema.sql:162`, `memorymaster/schema.sql:176`) | `BIGINT REFERENCES source_items(id) ON DELETE SET NULL` (`memorymaster/schema_postgres.sql:201`) | **MISMATCH**: integer width and FK syntax differ; referential action aligned. |
| `evidence_item_id` | `INTEGER` plus table FK set null (`memorymaster/schema.sql:163`, `memorymaster/schema.sql:177`) | `BIGINT REFERENCES evidence_items(id) ON DELETE SET NULL` (`memorymaster/schema_postgres.sql:202`) | **MISMATCH**: integer width and FK syntax differ; referential action aligned. |
| `claim_id` | `INTEGER` plus table FK set null (`memorymaster/schema.sql:164`, `memorymaster/schema.sql:178`) | `BIGINT REFERENCES claims(id) ON DELETE SET NULL` (`memorymaster/schema_postgres.sql:203`) | **MISMATCH**: integer width and FK syntax differ; referential action aligned. |
| `suggested_due_at` | `TEXT` (`memorymaster/schema.sql:165`) | `TIMESTAMPTZ` (`memorymaster/schema_postgres.sql:204`) | **MISMATCH**: timestamp representation differs. |
| `destination` | `TEXT NOT NULL DEFAULT 'manual'` (`memorymaster/schema.sql:166`) | `TEXT NOT NULL DEFAULT 'manual'` (`memorymaster/schema_postgres.sql:205`) | Aligned. |
| `status` | `TEXT NOT NULL DEFAULT 'candidate' CHECK (...)` (`memorymaster/schema.sql:167-168`) | `TEXT NOT NULL DEFAULT 'candidate' CHECK (...)` (`memorymaster/schema_postgres.sql:206-207`) | Aligned. |
| `confidence` | `REAL NOT NULL DEFAULT 0.5 CHECK (...)` (`memorymaster/schema.sql:169`) | `DOUBLE PRECISION NOT NULL DEFAULT 0.5 CHECK (...)` (`memorymaster/schema_postgres.sql:208`) | **MISMATCH**: numeric type differs, constraint aligned. |
| `payload_json` | `TEXT` (`memorymaster/schema.sql:170`) | `JSONB` (`memorymaster/schema_postgres.sql:209`) | **MISMATCH**: JSON storage differs. Likely intentional backend-native type mapping. |
| `exported_at` | `TEXT` (`memorymaster/schema.sql:171`) | `TIMESTAMPTZ` (`memorymaster/schema_postgres.sql:210`) | **MISMATCH**: timestamp representation differs. |
| `external_ref` | `TEXT` (`memorymaster/schema.sql:172`) | `TEXT` (`memorymaster/schema_postgres.sql:211`) | Aligned. |
| `idempotency_key` | `TEXT` (`memorymaster/schema.sql:173`) | `TEXT` (`memorymaster/schema_postgres.sql:212`) | Aligned. |
| `created_at` | `TEXT NOT NULL` (`memorymaster/schema.sql:174`) | `TIMESTAMPTZ NOT NULL` (`memorymaster/schema_postgres.sql:213`) | **MISMATCH**: timestamp representation differs. |
| `updated_at` | `TEXT NOT NULL` (`memorymaster/schema.sql:175`) | `TIMESTAMPTZ NOT NULL` (`memorymaster/schema_postgres.sql:214`) | **MISMATCH**: timestamp representation differs. |

### `mcp_usage`

SQLite table: `memorymaster/schema.sql:181-188`. Postgres table: `memorymaster/schema_postgres.sql:217-224`.

| Column | SQLite | Postgres | Alignment |
|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` (`memorymaster/schema.sql:182`) | `SERIAL PRIMARY KEY` (`memorymaster/schema_postgres.sql:218`) | **MISMATCH**: sequence implementation differs; Postgres uses `SERIAL`, not `BIGSERIAL` like most other tables. |
| `tool_name` | `TEXT NOT NULL` (`memorymaster/schema.sql:183`) | `TEXT NOT NULL` (`memorymaster/schema_postgres.sql:219`) | Aligned. |
| `timestamp` | `TEXT NOT NULL` (`memorymaster/schema.sql:184`) | `TEXT NOT NULL` (`memorymaster/schema_postgres.sql:220`) | Aligned; unlike other timestamp-like columns, Postgres does not use `TIMESTAMPTZ`. |
| `latency_ms` | `INTEGER` (`memorymaster/schema.sql:185`) | `INTEGER` (`memorymaster/schema_postgres.sql:221`) | Aligned. |
| `tenant_id` | `TEXT` (`memorymaster/schema.sql:186`) | `TEXT` (`memorymaster/schema_postgres.sql:222`) | Aligned. |
| `result_status` | `TEXT NOT NULL` (`memorymaster/schema.sql:187`) | `TEXT NOT NULL` (`memorymaster/schema_postgres.sql:223`) | Aligned. |

### `media_retry_queue`

SQLite table: `memorymaster/schema.sql:217-235`. Postgres table: `memorymaster/schema_postgres.sql:249-266`.

| Column | SQLite | Postgres | Alignment |
|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` (`memorymaster/schema.sql:218`) | `BIGSERIAL PRIMARY KEY` (`memorymaster/schema_postgres.sql:250`) | **MISMATCH**: integer width/sequence implementation differs. |
| `source_item_id` | `INTEGER NOT NULL` plus table FK cascade (`memorymaster/schema.sql:219`, `memorymaster/schema.sql:233`) | `BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE` (`memorymaster/schema_postgres.sql:251`) | **MISMATCH**: integer width and FK syntax differ; referential action aligned. |
| `media_key` | `TEXT NOT NULL` (`memorymaster/schema.sql:220`) | `TEXT NOT NULL` (`memorymaster/schema_postgres.sql:252`) | Aligned. |
| `chat_id` | `TEXT` (`memorymaster/schema.sql:221`) | `TEXT` (`memorymaster/schema_postgres.sql:253`) | Aligned. |
| `media_type` | `TEXT` (`memorymaster/schema.sql:222`) | `TEXT` (`memorymaster/schema_postgres.sql:254`) | Aligned. |
| `media_path` | `TEXT` (`memorymaster/schema.sql:223`) | `TEXT` (`memorymaster/schema_postgres.sql:255`) | Aligned. |
| `media_url` | `TEXT` (`memorymaster/schema.sql:224`) | `TEXT` (`memorymaster/schema_postgres.sql:256`) | Aligned. |
| `status` | `TEXT NOT NULL DEFAULT 'pending' CHECK (...)` (`memorymaster/schema.sql:225-226`) | `TEXT NOT NULL DEFAULT 'pending' CHECK (...)` (`memorymaster/schema_postgres.sql:257-258`) | Aligned. |
| `attempt_count` | `INTEGER NOT NULL DEFAULT 0` (`memorymaster/schema.sql:227`) | `INTEGER NOT NULL DEFAULT 0` (`memorymaster/schema_postgres.sql:259`) | Aligned. |
| `last_http_status` | `INTEGER` (`memorymaster/schema.sql:228`) | `INTEGER` (`memorymaster/schema_postgres.sql:260`) | Aligned. |
| `last_error` | `TEXT` (`memorymaster/schema.sql:229`) | `TEXT` (`memorymaster/schema_postgres.sql:261`) | Aligned. |
| `next_attempt_time` | `TEXT` (`memorymaster/schema.sql:230`) | `TIMESTAMPTZ` (`memorymaster/schema_postgres.sql:262`) | **MISMATCH**: timestamp representation differs. |
| `created_at` | `TEXT NOT NULL` (`memorymaster/schema.sql:231`) | `TIMESTAMPTZ NOT NULL` (`memorymaster/schema_postgres.sql:263`) | **MISMATCH**: timestamp representation differs. |
| `updated_at` | `TEXT NOT NULL` (`memorymaster/schema.sql:232`) | `TIMESTAMPTZ NOT NULL` (`memorymaster/schema_postgres.sql:264`) | **MISMATCH**: timestamp representation differs. |
| `UNIQUE (source_item_id, media_key)` | Present (`memorymaster/schema.sql:234`) | Present (`memorymaster/schema_postgres.sql:265`) | Aligned. |

## CHECK Constraints

| Constraint area | SQLite | Postgres | Parity |
|---|---|---|---|
| `claims.status` allowed states | Present (`memorymaster/schema.sql:14-16`) | Present (`memorymaster/schema_postgres.sql:21-23`) | Aligned. |
| `claims.confidence` range | Present (`memorymaster/schema.sql:17`) | Present (`memorymaster/schema_postgres.sql:24`) | Aligned. |
| `claims.pinned` boolean domain | `CHECK (pinned IN (0, 1))` (`memorymaster/schema.sql:18`) | Native `BOOLEAN`, no explicit CHECK (`memorymaster/schema_postgres.sql:25`) | Type-level equivalent, but CHECK is SQLite-only. |
| `source_items.sensitivity` allowed states | Present (`memorymaster/schema.sql:136`) | Present in table and compatibility alter (`memorymaster/schema_postgres.sql:177`, `memorymaster/schema_postgres.sql:242-243`) | Aligned, duplicated in Postgres migration path. |
| `evidence_items.confidence` range | Present (`memorymaster/schema.sql:150`) | Present (`memorymaster/schema_postgres.sql:190`) | Aligned. |
| `evidence_items.sensitivity` allowed states | Present (`memorymaster/schema.sql:152`) | Present in table and compatibility alter (`memorymaster/schema_postgres.sql:192`, `memorymaster/schema_postgres.sql:244-245`) | Aligned, duplicated in Postgres migration path. |
| `action_proposals.status` allowed states | Present (`memorymaster/schema.sql:167-168`) | Present (`memorymaster/schema_postgres.sql:206-207`) | Aligned. |
| `action_proposals.confidence` range | Present (`memorymaster/schema.sql:169`) | Present (`memorymaster/schema_postgres.sql:208`) | Aligned. |
| `media_retry_queue.status` allowed states | Present (`memorymaster/schema.sql:225-226`) | Present (`memorymaster/schema_postgres.sql:257-258`) | Aligned. |
| `claim_links.source_id <> target_id` | Table absent in SQLite (`memorymaster/schema.sql:1-239`) | Present (`memorymaster/schema_postgres.sql:278`) | **MISMATCH**: Postgres-only table/constraint. |
| `claim_links.link_type` allowed states | Table absent in SQLite (`memorymaster/schema.sql:1-239`) | Present (`memorymaster/schema_postgres.sql:279`) | **MISMATCH**: Postgres-only table/constraint. |

## Indexes

| Index | SQLite | Postgres | Parity |
|---|---|---|---|
| `idx_claims_status` | Present (`memorymaster/schema.sql:190`) | Present (`memorymaster/schema_postgres.sql:146`) | Aligned. |
| `idx_claims_updated_at` | Present (`memorymaster/schema.sql:191`) | Present (`memorymaster/schema_postgres.sql:147`) | Aligned. |
| `idx_claims_idempotency_key` | Unique, no partial predicate (`memorymaster/schema.sql:192`) | Unique, no partial predicate (`memorymaster/schema_postgres.sql:148`) | Aligned. |
| `idx_claims_tuple` | Present (`memorymaster/schema.sql:193`) | Present (`memorymaster/schema_postgres.sql:149`) | Aligned. |
| `idx_claims_replaced_by` | Present (`memorymaster/schema.sql:194`) | Present (`memorymaster/schema_postgres.sql:150`) | Aligned. |
| `idx_claims_human_id` | Unique (`memorymaster/schema.sql:199`) | Unique (`memorymaster/schema_postgres.sql:144`) | Aligned. |
| `idx_claims_tenant_id` | Absent because SQLite `claims.tenant_id` is absent (`memorymaster/schema.sql:3-35`, `memorymaster/schema.sql:190-211`) | Present (`memorymaster/schema_postgres.sql:145`) | **MISMATCH**: Postgres-only tenancy index. |
| `idx_citations_claim_id` | Present (`memorymaster/schema.sql:195`) | Present (`memorymaster/schema_postgres.sql:151`) | Aligned. |
| `idx_events_claim_id` | Present (`memorymaster/schema.sql:196`) | Present (`memorymaster/schema_postgres.sql:152`) | Aligned. |
| `idx_events_created_at` | Present (`memorymaster/schema.sql:197`) | Present (`memorymaster/schema_postgres.sql:153`) | Aligned. |
| `idx_embeddings_updated_at` | Present (`memorymaster/schema.sql:198`) | Present (`memorymaster/schema_postgres.sql:314`) | Aligned. |
| `idx_embeddings_vector` | Absent; SQLite stores `embedding_json` text (`memorymaster/schema.sql:106-112`, `memorymaster/schema.sql:190-211`) | Present only when `vector` extension exists (`memorymaster/schema_postgres.sql:286-300`) | **MISMATCH**: intentional Postgres vector-search feature. |
| `idx_external_sources_type` | Present (`memorymaster/schema.sql:200`) | Present (`memorymaster/schema_postgres.sql:226`) | Aligned. |
| `idx_source_items_source_id` | Present (`memorymaster/schema.sql:201`) | Present (`memorymaster/schema_postgres.sql:227`) | Aligned. |
| `idx_source_items_chat_id` | Present (`memorymaster/schema.sql:202`) | Present (`memorymaster/schema_postgres.sql:228`) | Aligned. |
| `idx_source_items_occurred_at` | Present (`memorymaster/schema.sql:203`) | Present (`memorymaster/schema_postgres.sql:229`) | Aligned. |
| `idx_source_items_content_hash` | Present (`memorymaster/schema.sql:204`) | Present (`memorymaster/schema_postgres.sql:230`) | Aligned. |
| `idx_source_items_sensitivity` | Static schema omits it and explains it is created by `_storage_schema._ensure_atlas_source_schema` (`memorymaster/schema.sql:212-215`) | Present (`memorymaster/schema_postgres.sql:246`) | **MISMATCH**: static schema drift; may be intentional migration-order difference, but not parity in these files. |
| `idx_evidence_items_source_item_id` | Present (`memorymaster/schema.sql:205`) | Present (`memorymaster/schema_postgres.sql:231`) | Aligned. |
| `idx_evidence_items_type` | Present (`memorymaster/schema.sql:206`) | Present (`memorymaster/schema_postgres.sql:232`) | Aligned. |
| `idx_evidence_items_sensitivity` | Static schema omits it and explains it is created by `_storage_schema._ensure_atlas_source_schema` (`memorymaster/schema.sql:212-215`) | Present (`memorymaster/schema_postgres.sql:247`) | **MISMATCH**: static schema drift; may be intentional migration-order difference, but not parity in these files. |
| `idx_action_proposals_status` | Present (`memorymaster/schema.sql:207`) | Present (`memorymaster/schema_postgres.sql:233`) | Aligned. |
| `idx_action_proposals_destination` | Present (`memorymaster/schema.sql:208`) | Present (`memorymaster/schema_postgres.sql:234`) | Aligned. |
| `idx_action_proposals_idempotency_key` | Unique partial index where key is not null (`memorymaster/schema.sql:209-211`) | Unique partial index where key is not null (`memorymaster/schema_postgres.sql:235-237`) | Aligned. |
| `idx_media_retry_status` | Present (`memorymaster/schema.sql:237`) | Present (`memorymaster/schema_postgres.sql:268`) | Aligned. |
| `idx_media_retry_next_attempt` | Present (`memorymaster/schema.sql:238`) | Present (`memorymaster/schema_postgres.sql:269`) | Aligned. |
| `idx_media_retry_source_item` | Present (`memorymaster/schema.sql:239`) | Present (`memorymaster/schema_postgres.sql:270`) | Aligned. |
| `idx_claim_links_unique` | Table absent in SQLite (`memorymaster/schema.sql:1-239`) | Present (`memorymaster/schema_postgres.sql:282`) | **MISMATCH**: Postgres-only relationship table/index. |
| `idx_claim_links_source` | Table absent in SQLite (`memorymaster/schema.sql:1-239`) | Present (`memorymaster/schema_postgres.sql:283`) | **MISMATCH**: Postgres-only relationship table/index. |
| `idx_claim_links_target` | Table absent in SQLite (`memorymaster/schema.sql:1-239`) | Present (`memorymaster/schema_postgres.sql:284`) | **MISMATCH**: Postgres-only relationship table/index. |

## One-Sided Constructs

| Construct | Side | Lines | Rationale / classification |
|---|---|---|---|
| `PRAGMA foreign_keys = ON` | SQLite-only | `memorymaster/schema.sql:1` | Intentional SQLite enforcement toggle; Postgres enforces FKs without an equivalent PRAGMA. |
| Confirmed tuple guard triggers | Both, different shape | SQLite has separate insert/update triggers (`memorymaster/schema.sql:37-70`); Postgres has one trigger function plus one trigger (`memorymaster/schema_postgres.sql:43-81`) | Functionally equivalent intent; implementation is backend-specific. |
| Events append-only triggers | Both, different shape | SQLite has separate update/delete triggers (`memorymaster/schema.sql:94-104`); Postgres has one trigger function plus two triggers (`memorymaster/schema_postgres.sql:103-142`) | Functionally equivalent intent; implementation is backend-specific. |
| `CREATE EXTENSION IF NOT EXISTS vector` guarded by `DO $$` | Postgres-only | `memorymaster/schema_postgres.sql:1-8` | Intentional Postgres vector capability probe. No SQLite equivalent in this schema. |
| Conditional `claim_embeddings` table definition | Postgres-only branching | `memorymaster/schema_postgres.sql:286-312` | Intentional Postgres native-vector optimization with text fallback. SQLite has one static JSON-text table (`memorymaster/schema.sql:106-112`). |
| `idx_embeddings_vector` HNSW index | Postgres-only | `memorymaster/schema_postgres.sql:297-300` | Intentional Postgres vector-search acceleration. SQLite schema has no equivalent index (`memorymaster/schema.sql:190-211`). |
| Compatibility `ALTER TABLE claims ADD COLUMN IF NOT EXISTS` | Postgres-only | `memorymaster/schema_postgres.sql:37-41` | Migration compatibility for existing Postgres installs; SQLite file is pure create schema for these columns (`memorymaster/schema.sql:3-35`). |
| Compatibility sensitivity `ALTER TABLE` statements | Postgres-only | `memorymaster/schema_postgres.sql:238-245` | Migration compatibility; SQLite static schema defers related sensitivity indexes to `_storage_schema._ensure_atlas_source_schema` according to comments (`memorymaster/schema.sql:212-215`). |
| `claim_links` table and indexes | Postgres-only | Table/checks at `memorymaster/schema_postgres.sql:272-280`; indexes at `memorymaster/schema_postgres.sql:282-284` | **Gap to close** unless relationship storage is intentionally Postgres-only. Prior parity expectations suggest SQLite should expose matching persistent link storage. |
| FTS5 virtual tables | Neither schema file | SQLite file range `memorymaster/schema.sql:1-239`; Postgres file range `memorymaster/schema_postgres.sql:1-314` | No SQLite FTS5 table is present in this schema file, and no Postgres `tsvector` or GIN text-search equivalent is present. This is a parity note, not a one-sided construct. |

## Recommended Follow-Up Tracks

1. **T11-F1: Decide `claims` column authority.** Add or intentionally document the Postgres omissions for `tier`, `access_count`, `last_accessed`, `event_time`, `valid_from`, and `valid_until` (`memorymaster/schema.sql:26-31`, `memorymaster/schema_postgres.sql:10-35`). Add or intentionally document SQLite omission of `tenant_id` (`memorymaster/schema.sql:3-35`, `memorymaster/schema_postgres.sql:33`).
2. **T11-F2: Normalize relationship schema.** Either add SQLite DDL for `claim_links` or mark the Postgres table as intentionally backend-specific in storage docs/tests (`memorymaster/schema.sql:1-239`, `memorymaster/schema_postgres.sql:272-284`).
3. **T11-F3: Make sensitivity index parity explicit.** The SQLite schema comments say sensitivity indexes are created elsewhere, while Postgres creates them in-schema (`memorymaster/schema.sql:212-215`, `memorymaster/schema_postgres.sql:246-247`). Add a generated-schema note or test that verifies the effective SQLite database has these indexes after initialization.
4. **T11-F4: Lock timestamp/JSON type policy.** Document backend-native mappings from SQLite `TEXT` to Postgres `TIMESTAMPTZ`/`JSONB` for timestamp and JSON columns, or add adapter tests that prove round-trip semantics remain aligned. Examples include events payloads (`memorymaster/schema.sql:89`, `memorymaster/schema_postgres.sql:99`) and source item timestamps (`memorymaster/schema.sql:132`, `memorymaster/schema_postgres.sql:173`).
5. **T11-F5: Clarify embedding backend policy.** Postgres uses `VECTOR(1536)` plus HNSW when the `vector` extension is available and falls back to `embedding_json`; SQLite always stores `embedding_json` text (`memorymaster/schema.sql:106-112`, `memorymaster/schema_postgres.sql:286-314`). Add documentation/tests for both Postgres branches.
6. **T11-F6: Decide full-text search direction.** Since neither schema file contains SQLite FTS5 nor Postgres `tsvector`/GIN search DDL (`memorymaster/schema.sql:1-239`, `memorymaster/schema_postgres.sql:1-314`), either keep full-text search out of schema parity scope or add explicit, tested DDL in both backends.
