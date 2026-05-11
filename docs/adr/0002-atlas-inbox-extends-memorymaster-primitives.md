# 0002 Atlas Inbox Extends MemoryMaster Primitives

Date: 2026-05-05

Status: Accepted

Source Claims: claim #36274, claim #36387

## Context

Atlas Inbox adds external source ingestion, evidence records, and action proposal workflows. A competing design would create a separate product database and parallel workflow system.

MemoryMaster already has append-only events, claims, citations, an operator dashboard, steward processing, and durable lifecycle state.

## Decision

Atlas Inbox must extend existing MemoryMaster primitives instead of creating a disconnected product database. Atlas records live in the same MemoryMaster database as claims and citations.

Source items, evidence items, media retry state, action proposals, and claim outputs are part of the MemoryMaster persistence model.

## Consequences

Atlas features can reuse MemoryMaster auditability, lifecycle handling, and operator tooling.

The design avoids duplicate persistence and reduces integration drift between imported source material and generated claims.

Consumers must point at the same MemoryMaster database path for Atlas and claim operations.
