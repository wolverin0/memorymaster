# STRUCTURE.md — Directory Structure

```
/mnt/pyapps/memorymaster/
├── pyproject.toml              # Build config, deps, entry points (v2.0.0)
├── pytest.ini                  # Test config
├── memorymaster.db             # Default SQLite database
├── README.md / USER_GUIDE.md  # Docs
├── CHANGELOG.md / ROADMAP.md  # History & plans
├── ARCHITECTURE.md             # High-level design doc
├── COMPETITOR_ANALYSIS.md
│
├── memorymaster/               # Main package
│   ├── __init__.py
│   ├── __main__.py
│   ├── service.py              # MemoryService — primary API surface
│   ├── models.py               # Dataclasses: Claim, Citation, Event, ClaimLink; validators
│   ├── storage.py              # SQLiteStore — full SQLite implementation
│   ├── postgres_store.py       # PostgreSQL store
│   ├── store_factory.py        # create_store() — selects backend from db_target
│   ├── schema.py               # load_schema_sql() helper
│   ├── schema.sql              # SQLite DDL
│   ├── schema_postgres.sql     # PostgreSQL DDL
│   ├── cli.py                  # CLI entry point (argparse)
│   ├── mcp_server.py           # MCP server (FastMCP tools)
│   ├── dashboard.py            # TUI dashboard
│   ├── llm_steward.py          # LLM-based steward daemon
│   ├── steward.py              # Steward logic helpers
│   ├── lifecycle.py            # Lifecycle state machine helpers
│   ├── policy.py               # Revalidation candidate selection
│   ├── retrieval.py            # rank_claim_rows, VectorSearchHook
│   ├── embeddings.py           # EmbeddingProvider, cosine_similarity
│   ├── qdrant_backend.py       # Qdrant REST integration
│   ├── security.py             # Sensitive claim detection, encryption, sanitization
│   ├── conflict_resolver.py    # Conflict detection logic
│   ├── context_optimizer.py    # pack_context() — token-budget greedy knapsack
│   ├── config.py               # Configuration loading
│   ├── retry.py                # connect_with_retry()
│   ├── review.py               # Review queue helpers
│   ├── scheduler.py            # Job scheduling
│   ├── snapshot.py             # Snapshot/export utilities
│   ├── metrics_exporter.py     # Metrics export
│   ├── operator.py             # Operator interface
│   ├── operator_queue.py       # Operator queue management
│   ├── turn_schema.py          # Turn/conversation schema
│   │
│   └── jobs/                   # Lifecycle jobs
│       ├── __init__.py
│       ├── extractor.py        # Claim metadata extraction
│       ├── validator.py        # Citation/score-based validation
│       ├── deterministic.py    # Rule-based deterministic validation
│       ├── decay.py            # Confidence decay
│       ├── compactor.py        # Archival + event pruning
│       ├── compact_summaries.py # LLM-based summary compaction
│       ├── dedup.py            # Deduplication via embeddings
│       └── staleness.py        # Staleness detection
│
├── tests/                      # Test suite (43 files)
│   ├── conftest.py
│   └── test_*.py
│
├── artifacts/                  # Compaction artifacts, benchmarks output
├── benchmarks/                 # Benchmark scripts
├── scripts/                    # Utility scripts
├── data/                       # Sample/fixture data
├── docs/                       # Extended documentation
└── dist/                       # Build output
```
