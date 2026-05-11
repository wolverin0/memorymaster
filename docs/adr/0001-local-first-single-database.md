# 0001 Local-First Single Database

Date: 2026-05-05

Status: Accepted

Source Claims: claim #35163, claim #34858, claim #35039, claim #36387

## Context

MemoryMaster needs durable memory without requiring an external service for the default deployment. It also needs concurrent agent access and a clear boundary for consumers that integrate Atlas Inbox data.

The claims record three connected decisions: use a single-file SQLite database with WAL mode, enforce explicit concurrency handling, and keep all MemoryMaster tables in one database file. The single database includes claims, citations, events, links, embeddings, Atlas source tables, evidence tables, and action proposals.

## Decision

MemoryMaster is local-first by default and uses one database file for all MemoryMaster tables. SQLite with WAL mode is the default persistence layer, and callers must treat the `--db` path as the complete MemoryMaster database, not as a claims-only database or an Atlas-only database.

Postgres parity may exist as an implementation target, but the architecture still models claims and Atlas tables as one logical store.

## Consequences

This keeps installation and operation simple for local AI-agent workflows and avoids server round trips in the normal path.

Consumers must not create a split layout such as one database for claims and another for Atlas Inbox. A split layout creates disconnected state and silently breaks the claim/source relationship.

Schema and concurrency changes must preserve WAL behavior and the all-tables-in-one-store assumption.
