# Prioritized Remaining-Blocker Ledger

This is a release-oriented view. `external-actions-required.md` remains the
canonical owner/evidence ledger.

| Priority | Finding(s) | Blocking evidence/action |
|---|---|---|
| P0 | MM-OPS-01, MM-OPS-04 | Rotate/classify historical credentials and Gitleaks findings; complete strict dependency and exact release-image scans |
| P0 | MM-SEC-01 | Disposable two-role Postgres RLS/parity proof and approved brownfield inventory/repair evidence |
| P0 | MM-SEC-02 | Disposable authenticated/TLS Qdrant governed retrieval, reconciliation, outbox, sync, and delete proof |
| P0 | MM-SEC-03 | Authorized aggregate legacy-store inventory completion, including Qdrant; separately approve any cleanup/redaction |
| P1 | MM-OPS-02, MM-OPS-03 | Approved immutable images, disposable Kubernetes/Helm proof, then explicit tag/PyPI workflow approval |
| P1 | MM-OPS-05 | Approved off-device encrypted destination plus SQLite/Postgres restore drill |
| P1 | MM-PRIV-01, MM-PRIV-02 | Product/legal consent, processor, retention, attribution, export/erase disposition, and backend dry-run decisions |
| P2 | MM-CAP-01 | Address or monitor host-storage capacity without deleting product data |

No blocker above authorizes a mutation. Each external owner must approve the
specific action and retain the evidence requested by the canonical ledger.
