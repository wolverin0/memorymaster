# 0009 Schema Fallback Is Not Migration Success

Date: 2026-05-06

Status: Accepted

Source Claims: claim #36532

## Context

`storage.py:init_db` has a lenient fallback path. If executing the full schema script fails, it logs a warning and retries individual statements while suppressing operational errors.

That fallback can keep initialization from crashing, but it can also hide schema migration bugs by returning success while some DDL failed.

## Decision

The lenient fallback is a defense-in-depth safety net, not proof that a migration is correct.

After any schema or migration-helper change, tests must exercise initialization against a stale database and assert that no `lenient schema initialization` warning appears.

## Consequences

Schema changes need stale-database coverage, not only fresh-database coverage.

Warnings from the lenient fallback should be treated as migration risk.

The fallback may remain for partial initialization scenarios, but it must not normalize sloppy migrations.
