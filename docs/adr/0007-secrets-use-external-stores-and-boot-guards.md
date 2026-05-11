# 0007 Secrets Use External Stores and Boot Guards

Date: 2026-05-02

Status: Accepted

Source Claims: claim #30226, claim #32353, claim #32358, claim #35968

## Context

MemoryMaster integrates with multiple LLM providers and external systems. Credentials must not be hardcoded, stored in claims, or committed to the repository.

The claims also identify repository cleanup and secret rotation as mandatory remediation when exposure is found.

## Decision

Sensitive credentials must be managed through environment variables or external secret managers such as vaults and secure credential stores.

Production startup must validate required configuration and secrets through a boot guard pattern. If required configuration is missing or invalid, startup should fail early.

## Consequences

Deployments become more explicit and less likely to run with accidental fallback credentials.

Credential exposure has a defined remediation path: rotate secrets and clean repository state.

Tests and setup templates should keep validating configuration behavior instead of silently substituting unsafe defaults.
