# 0001 Local-First Single Database

Date: 2026-05-05; reaffirmed 2026-07-14

Status: Accepted

Source Claims: claim #35163, claim #34858, claim #35039, claim #36387,
`mm-69b9~3`

## Context

MemoryMaster needs durable memory without requiring an external service for the default deployment. It also needs concurrent agent access and a clear boundary for consumers that integrate Atlas Inbox data.

The claims record three connected decisions: use a single-file SQLite database with WAL mode, enforce explicit concurrency handling, and keep all MemoryMaster tables in one database file. The single database includes claims, citations, events, links, embeddings, Atlas source tables, evidence tables, and action proposals.

## Decision

MemoryMaster is local-first by default and uses one database file for all MemoryMaster tables. SQLite with WAL mode is the default persistence layer, and callers must treat the `--db` path as the complete MemoryMaster database, not as a claims-only database or an Atlas-only database.

Postgres parity may exist as an implementation target, but the architecture still models claims and Atlas tables as one logical store.

The primary supported product posture is personal/local use. Shared multi-user
operation is not part of the current product target. The Postgres/team profile
is retained as an optional future capability and must remain disabled until its
separate two-role, row-level-security, and deployment evidence is complete.
That deferred profile does not make Postgres a dependency of the local product.

Qdrant is also optional. It may augment local SQLite recall with governed
semantic candidates, but it is never authoritative and is not required by the
minimal profile.

## Consequences

This keeps installation and operation simple for local AI-agent workflows and avoids server round trips in the normal path.

Local release decisions are evaluated against the SQLite minimal profile.
Unproven Postgres/team, Qdrant/semantic, or Kubernetes profiles stay visibly
disabled and documented, but do not block an accurately scoped local release.

Consumers must not create a split layout such as one database for claims and another for Atlas Inbox. A split layout creates disconnected state and silently breaks the claim/source relationship.

Schema and concurrency changes must preserve WAL behavior and the all-tables-in-one-store assumption.
