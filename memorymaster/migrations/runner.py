"""MigrationRunner — discovers, sorts, applies, and drift-checks migrations.

Backend-agnostic: the runner takes an open DB connection and a ``backend``
tag (``"sqlite"`` or ``"postgres"``), and dispatches each migration to
its ``apply_sqlite`` or ``apply_postgres`` function.

The ``schema_versions`` table is auto-created on first run (no migration
required for the runner's own bookkeeping table — chicken-and-egg).
"""
from __future__ import annotations

import datetime
import hashlib
import importlib
import logging
import pkgutil
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Migration filenames look like 0001_short_description.py
_FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.py$")

# DDL for the bookkeeping table, applied separately on first run.
# Same shape on both backends — INTEGER/TEXT vs INTEGER/TEXT works on
# SQLite and Postgres without dialect divergence here.
_SCHEMA_VERSIONS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS schema_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL UNIQUE,
    description TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
""".strip()

_SCHEMA_VERSIONS_DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS schema_versions (
    id SERIAL PRIMARY KEY,
    version INTEGER NOT NULL UNIQUE,
    description TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
""".strip()


class MigrationDriftError(RuntimeError):
    """Raised when an applied migration's file source differs from the
    checksum stored in ``schema_versions`` — migrations are immutable
    once applied; modifying them after the fact is a hard error."""


@dataclass(frozen=True)
class Migration:
    """One discovered migration module."""

    version: int
    description: str
    module_name: str
    source_path: Path
    apply_sqlite: Callable[[Any], None]
    apply_postgres: Callable[[Any], None]

    def checksum(self) -> str:
        """sha256 hex of the migration file's source bytes."""
        return hashlib.sha256(self.source_path.read_bytes()).hexdigest()


def discover_migrations(package: str = "memorymaster.migrations") -> list[Migration]:
    """Return all migrations in ``package``, sorted by version ascending.

    A migration module must define ``VERSION`` (int), ``DESCRIPTION`` (str),
    ``apply_sqlite(conn)``, and ``apply_postgres(conn)``. Files that don't
    match the ``NNNN_*.py`` naming or lack the required attributes are
    silently skipped — keeps the package importable while WIP migrations
    exist.
    """
    pkg = importlib.import_module(package)
    pkg_path = Path(pkg.__file__).parent

    migrations: list[Migration] = []
    for module_info in pkgutil.iter_modules([str(pkg_path)]):
        if not _FILENAME_RE.match(f"{module_info.name}.py"):
            continue
        full_name = f"{package}.{module_info.name}"
        module = importlib.import_module(full_name)
        try:
            version = int(module.VERSION)
            description = str(module.DESCRIPTION)
            apply_sqlite = module.apply_sqlite
            apply_postgres = module.apply_postgres
        except (AttributeError, ValueError, TypeError) as exc:
            logger.warning(
                "migrations: skipping %s — missing/invalid required attrs: %s",
                module_info.name,
                exc,
            )
            continue

        source_path = pkg_path / f"{module_info.name}.py"
        migrations.append(
            Migration(
                version=version,
                description=description,
                module_name=full_name,
                source_path=source_path,
                apply_sqlite=apply_sqlite,
                apply_postgres=apply_postgres,
            )
        )

    migrations.sort(key=lambda m: m.version)

    # Sanity: versions must be unique
    seen: set[int] = set()
    for m in migrations:
        if m.version in seen:
            raise RuntimeError(
                f"duplicate migration version {m.version} in {package}"
            )
        seen.add(m.version)

    return migrations


@dataclass(frozen=True)
class MigrationStatus:
    version: int
    description: str
    applied: bool
    applied_at: str | None
    stored_checksum: str | None
    current_checksum: str


class MigrationRunner:
    """Apply pending migrations and report status.

    Usage::

        runner = MigrationRunner(conn, backend="sqlite")
        runner.apply_pending()         # apply everything not yet recorded
        for s in runner.status():
            print(s.version, s.applied, s.description)
    """

    def __init__(self, conn: Any, *, backend: str) -> None:
        if backend not in {"sqlite", "postgres"}:
            raise ValueError(f"backend must be 'sqlite' or 'postgres', got {backend!r}")
        self.conn = conn
        self.backend = backend
        self._ensure_schema_versions_table()

    # ----- bookkeeping table ---------------------------------------------

    def _ensure_schema_versions_table(self) -> None:
        ddl = (
            _SCHEMA_VERSIONS_DDL_SQLITE
            if self.backend == "sqlite"
            else _SCHEMA_VERSIONS_DDL_POSTGRES
        )
        self._execute(ddl)
        self._commit()

    def _execute(self, sql: str, params: tuple = ()) -> Any:
        if self.backend == "sqlite":
            return self.conn.execute(sql, params)
        # psycopg connection: open a cursor inline
        cur = self.conn.cursor()
        # Psycopg uses %s placeholders, not ?. The runner's own SQL uses
        # neither — we only parameterize for INSERT INTO schema_versions
        # below, which uses %s for postgres and ? for sqlite via _insert_record.
        cur.execute(sql, params)
        return cur

    def _commit(self) -> None:
        commit = getattr(self.conn, "commit", None)
        if callable(commit):
            commit()

    def _query_applied(self) -> dict[int, tuple[str, str, str]]:
        """Return {version: (description, checksum, applied_at)} for applied migrations."""
        cur = self._execute("SELECT version, description, checksum, applied_at FROM schema_versions")
        out: dict[int, tuple[str, str, str]] = {}
        for row in cur.fetchall():
            # SQLite Row + Postgres dict_row both support either index or key access.
            if isinstance(row, dict):
                version = int(row["version"])
                out[version] = (row["description"], row["checksum"], row["applied_at"])
            else:
                version = int(row[0])
                out[version] = (row[1], row[2], row[3])
        return out

    def _insert_record(self, version: int, description: str, checksum: str, applied_at: str) -> None:
        if self.backend == "sqlite":
            self.conn.execute(
                "INSERT INTO schema_versions(version, description, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (version, description, checksum, applied_at),
            )
        else:
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO schema_versions(version, description, checksum, applied_at) VALUES (%s, %s, %s, %s)",
                (version, description, checksum, applied_at),
            )
        self._commit()

    # ----- public API ----------------------------------------------------

    def apply_pending(self) -> list[int]:
        """Apply every migration whose version is not yet recorded.

        Returns the list of versions newly applied. Raises ``MigrationDriftError``
        when a previously-applied migration's source has changed.
        """
        applied = self._query_applied()
        migrations = discover_migrations()

        # Drift check: every applied version that still has a file on disk
        # must match the stored checksum.
        for m in migrations:
            if m.version in applied:
                stored_desc, stored_checksum, _ = applied[m.version]
                if stored_checksum != m.checksum():
                    raise MigrationDriftError(
                        f"migration v{m.version:04d} ({m.description!r}) source has changed since it was applied. "
                        f"Stored checksum={stored_checksum[:12]}…, current={m.checksum()[:12]}…. "
                        f"Migrations are immutable once applied — revert your edits or write a new migration."
                    )

        newly_applied: list[int] = []
        for m in migrations:
            if m.version in applied:
                continue
            logger.info("migrations: applying v%04d — %s", m.version, m.description)
            apply_fn = m.apply_sqlite if self.backend == "sqlite" else m.apply_postgres
            apply_fn(self.conn)
            self._insert_record(
                m.version,
                m.description,
                m.checksum(),
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
            newly_applied.append(m.version)
        return newly_applied

    def status(self) -> list[MigrationStatus]:
        """Return per-migration status: known, applied, drift indicator."""
        applied = self._query_applied()
        out: list[MigrationStatus] = []
        for m in discover_migrations():
            entry = applied.get(m.version)
            out.append(
                MigrationStatus(
                    version=m.version,
                    description=m.description,
                    applied=entry is not None,
                    applied_at=(entry[2] if entry else None),
                    stored_checksum=(entry[1] if entry else None),
                    current_checksum=m.checksum(),
                )
            )
        return out
