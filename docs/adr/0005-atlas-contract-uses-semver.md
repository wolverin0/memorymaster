# 0005 Atlas Contract Uses SemVer
# Decision: external Atlas consumers negotiate a stable SemVer contract.
# Key terms: Atlas, SemVer, compatibility, producer, consumer, contract.
# Read when: changing capture envelopes, review payloads, or versions.
# Status: Accepted on 2026-05-05; source claim #36342.
# Boundary: consumer branding does not form part of the public contract.
# Updated: 2026-07-30 after retiring a named legacy consumer integration.

## Context

Atlas has CLI and HTTP consumers. Those consumers need a reliable way to detect incompatible changes before they start processing data.

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
