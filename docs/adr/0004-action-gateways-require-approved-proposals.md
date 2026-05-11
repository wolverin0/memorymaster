# 0004 Action Gateways Require Approved Proposals

Date: 2026-05-05

Status: Accepted

Source Claims: claim #36280

## Context

Atlas Inbox may integrate with external action systems such as One CLI, Pica, Zapier, Make, n8n, MCP servers, or native APIs. Those systems can mutate external state, so direct execution from raw imported content would be unsafe.

## Decision

Atlas Inbox must create source-backed action proposals before using external action gateways. Gateway execution is allowed only after the proposal is tied to source evidence and the user approves it.

## Consequences

Action execution has an auditable chain from imported source item to proposal to approval to external side effect.

The architecture separates memory extraction from actuation. This makes integrations safer and easier to review.

Implementations must not bypass the proposal and approval layer for convenience.
