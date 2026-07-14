# Prioritized Remaining-Blocker Ledger

This is a release-oriented view for the primary personal/local SQLite profile.
`external-actions-required.md` remains the canonical owner/evidence ledger.

## Blocks publishing the minimal package

| Priority | Finding(s) | Required evidence/action |
|---|---|---|
| P0 | MM-OPS-04 | Classify the unsuppressed history findings and complete a strict dependency audit for the exact minimal release extras |
| P0 | MM-OPS-03 | Execute the tag-triggered verified-artifact workflow only after explicit approval; publish the exact promoted bytes |
| P1 | MM-SEC-03, MM-DATA-01 | Before migrating or remediating the user's active SQLite DB, approve and verify a backup plus read-only inventory; not a package-publication gate |
| P2 | MM-CAP-01 | Address or monitor host-storage capacity without deleting product data |

## Deferred optional-profile evidence

| Profile | Findings | Deferred evidence |
|---|---|---|
| Team/Postgres | MM-SEC-01, MM-OPS-01 | Two-role RLS/parity, brownfield inventory, credential/network, and Postgres recovery proof |
| Semantic/Qdrant | MM-SEC-02 | Authenticated/TLS governed candidate, reconciliation, outbox, sync, and delete proof |
| Hosted/container | MM-OPS-02, MM-OPS-04 image scan | Approved immutable images, scans, Kubernetes/Helm runtime and network-policy proof |
| Organization/privacy | MM-PRIV-01, MM-PRIV-02 | Organizational consent, processor, retention, export/erase, and backend-disposition decisions |
| Extended recovery | MM-OPS-05 | Approved off-device destination and PostgreSQL restore drill |

No blocker above authorizes a mutation. Each external owner must approve the
specific action and retain the evidence requested by the canonical ledger.
