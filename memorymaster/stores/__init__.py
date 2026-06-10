"""Persistence layer: SQLite store + mixins, Postgres store, store factory,
snapshots, and versioned schema migrations.

P2 restructure subpackage. Hosts the SQLiteStore facade and its mixin
modules (``_storage_*``), the PostgresStore parity backend, the DSN-routing
store factory, git-backed DB snapshots, and the ``migrations`` package
(versioned, checksummed, immutable-once-applied).
"""
