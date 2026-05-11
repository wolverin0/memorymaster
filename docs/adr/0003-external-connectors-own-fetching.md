# 0003 External Connectors Own Fetching

Date: 2026-05-05

Status: Accepted

Source Claims: claim #36279, claim #36388, claim #36396

## Context

External sources such as WhatsApp media, Gmail attachments, documents, and calendar invites often require credentials, OAuth, IP allowlists, captcha handling, or API-specific retry behavior. MemoryMaster media processing currently expects a local media path and does not fetch HTTP assets itself.

The Atlas Inbox WhatsApp path identified `wacli` as the first live import target and `wacrawl` as a later read-only archive or encrypted Git backup adapter.

## Decision

MemoryMaster owns durable connector state, not external fetching. It stores queue rows, status transitions, audit events, and idempotency records. The consumer environment owns fetching and reports outcomes back to MemoryMaster.

The connector handshake is:

1. Consumer enqueues work.
2. MemoryMaster atomically claims and returns pending rows.
3. Consumer fetches or processes the external resource.
4. Consumer records the per-row outcome.

HTTP expiration conditions such as 403 or 410 are terminal source states, not MemoryMaster fetch failures.

## Consequences

Credentials and API-specific behavior stay in the consumer container where they belong.

MemoryMaster remains a stateful contract and processing system instead of becoming a general HTTP fetcher, OAuth client, and rate-limit manager.

Future connectors should follow the same state/fetch split unless MemoryMaster deliberately grows a connector-specific fetch subsystem.
