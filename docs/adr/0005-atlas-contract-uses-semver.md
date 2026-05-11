# 0005 Atlas Contract Uses SemVer

Date: 2026-05-05

Status: Accepted

Source Claims: claim #36342

## Context

Atlas has CLI and HTTP consumers, including LifeAgent. Consumers need a reliable way to detect incompatible changes before they start processing data.

The Atlas v1 contract introduced a discoverable contract payload through the `atlas-version` CLI and the `/api/atlas/version` HTTP endpoint.

## Decision

Atlas API and CLI contracts use semantic versioning.

MAJOR changes include removed or renamed CLI flags, removed envelope fields, changed field types or semantics, removed endpoints, and changed HTTP methods.

MINOR changes are additive only, such as new subcommands, endpoints, or envelope fields.

PATCH changes are behavioral fixes.

Consumers must refuse startup on a major contract mismatch. Atlas command responses include contract metadata through the shared JSON envelope pattern.

## Consequences

Consumers can fail fast instead of corrupting or misreading imported state.

Additive changes remain possible without breaking existing consumers.

Contract shape tests must stay authoritative for the metadata envelope and Atlas command responses.
