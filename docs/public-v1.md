<!-- doc-head: public v1 facade with opt-in skills and graph observations -->
# Public v1: remember, recall, forget, improve
# Covers: stable Python, CLI, and MCP contracts for governed personal memory.
# Key terms: trusted recall, approved skills, derived observations, logical retirement.
# Read when: integrating a producer, capturing a file, or building a friendly client.
# Defaults: confirmed claims only; skills and observations off; retirement preview only.
<!-- /doc-head -->

MemoryMaster’s friendly facade does not bypass claim governance. Capture stores
source and evidence synchronously, then queues extraction. Extracted claims are
candidates until the existing steward confirms them.

## Python

```python
from memorymaster import forget, improve, recall, remember

receipt = remember(
    text="Alice participates in Project Atlas.",
    source_uri="https://producer.example/items/42",
    scope="project:atlas",
    source_agent="my-producer",
)

context = recall(
    "What does Alice participate in?",
    scope_allowlist=["project:atlas"],
    token_budget=4000,
    include_skills=True,
    skill_limit=3,
    include_observations=True,
    observation_limit=2,
)

preview = forget(source_item_id=receipt.source_item["id"])
queued = improve(scope="project:atlas", max_items=200)
```

The response contract is versioned as `memorymaster.public.v1`. `remember`
returns source, evidence, job IDs, replay/deduplication state, and warnings.
`recall` returns rendered context plus claim IDs, citations, lifecycle state,
score explanations, plus separate `skills` and `observations` tuples.
`include_skills=True` adds complete
confirmed skills authorized for the requested scopes as an explicit text
section while sharing the same token budget. It excludes raw skill JSON from
ordinary claim context. The option defaults off; candidate recall still
requires `trust_mode="exploratory"`, and candidate skills are never projected.
`include_observations=True` adds a separately packed `DERIVED OBSERVATIONS`
section. Trusted mode revalidates exact support and returns confirmed
observations only; exploratory mode may label candidate or stale observations.
Observations never enter the ordinary claim list, and `observation_limit` is
bounded to five. The section reserves at most 25% or 800 tokens of the same
recall budget.

## CLI and MCP

```bash
memorymaster --workspace . remember --text "A governed observation."
memorymaster --workspace . remember --file .\notes\decision.md
memorymaster --workspace . remember --url https://example.com/reference
memorymaster --workspace . recall "governed observation" --include-observations
memorymaster --workspace . forget --claim-id 42
memorymaster --workspace . forget --source-item-id 7 --apply
memorymaster --workspace . improve --scope project:example
```

MCP exposes the same four operation names and response fields, including
`recall(include_observations=true, observation_limit=2)`. Existing advanced
commands and tools remain available.

## Capture boundary

- Inline text, UTF-8/UTF-8-BOM text/Markdown, deterministic local HTML, and
  optional PDF/DOCX are supported.
- Install `memorymaster[capture]` for PDF and DOCX.
- Images and audio require explicitly configured real OCR/transcription
  providers. Provider absence becomes an actionable blocked job.
- MemoryMaster never fetches URLs. A URL-only capture is retained as
  `awaiting_evidence`; producers own authentication and fetching.
- Local files are accepted only in private/local-trusted mode, must resolve
  under `MEMORYMASTER_CAPTURE_ROOTS`, and cannot escape through symlinks.
- Original local documents are not copied. MemoryMaster stores root-relative
  locators, hashes, MIME metadata, and governed extracted evidence.

## Retirement semantics

`forget` previews by default. A direct claim target archives through the
canonical lifecycle. Retiring a source preserves evidence and audit history:

- a candidate with no other active evidence becomes archived;
- a confirmed claim with no other active evidence becomes stale and leaves
  trusted recall;
- a claim with another active source remains active.

This is logical retirement, not privacy erasure. Use the existing redaction or
erasure workflow when the requirement is removal of sensitive payloads.

## Background processing and visibility

`improve` queues due claim extraction, steward review, confirmed-claim graph
work, and observation discovery. It never runs synthesis, confirms, or rewrites
a claim in the caller's request. Its queue receipt reports observation discover
and synthesis counts separately. The existing Dreaming schedule drains the
bounded queue under the shared provider budgets.

Capture status, evidence lineage, claims, citations, graph supports, and source
retirement are visible in the Capture Inbox. The Derived Observations panel
shows lifecycle status, type, evidence window, exact supporting
claims/evidence/relationships, diagnostics, and lifecycle history.

Run `memorymaster --json demo` for a deterministic temporary-database example
that promotes a cited three-blocker observation and then proves automatic
staleness after support retirement.
