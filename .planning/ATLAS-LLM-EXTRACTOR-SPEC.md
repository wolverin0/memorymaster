# Spec — LLM Typed-Entity Atlas Extractor

**Status:** DRAFT → building (full autonomy granted).
**Why:** The current `bridges/atlas_claim_extractor.py` is a deterministic keyword-matcher. On the live VM it produced 5,544 misclassified subject-line wrappers ("Atlas commitment evidence from vercel[bot]: Re:[repo]") with `subject="whatsapp_contact"` and `whatsapp://` citations even for email — noise, not knowledge. This is why the user's "AI that knows my life" never materialized (claim `mm-d993`). The connectors + evidence harvest are solid; only extraction is the weak link.
**Goal:** Replace the extractor with an LLM pass that reads evidence **bodies** (+ sender + date) and emits 0..N **typed life-knowledge** claims (person/company/project/commitment/decision/preference/fact/event), or **nothing** for newsletters/bot-notifications. This is also the Karpathy typed-entity model (P1+P2+P3 converge).

**Scope of THIS build:** generic capability code in the public MM repo + hermetic tests with synthetic fixtures (NO personal data). Running it over real evidence on the VM is a separate private activation step after merge.

---

## 1. New module — `memorymaster/bridges/atlas_llm_extractor.py`

```python
def extract_atlas_claims_llm(
    service, *, scope: str, limit: int = 200, model: str | None = None, dry_run: bool = False
) -> AtlasClaimExtractionResult:
    ...
```
Reuse the existing `AtlasClaimExtractionResult` dataclass (import from `atlas_claim_extractor`) so the CLI/bridge contract is unchanged. Add `degraded` + `emitted` counters to the result dict (extend `to_dict`, keep existing keys).

Per evidence item (`service.list_evidence_items(limit=...)`):
1. Load the `SourceItem` (`service.get_source_item_by_id`) for sender_name / occurred_at / provider.
2. Build a prompt (see §2) from provider + sender + date + the evidence `text` (the actual body).
3. `raw = call_llm(prompt, text)` (from `memorymaster.core.llm_provider`). Follow the JSON-parsing convention already in `govern/jobs/extractor.py` (strip ```json fences, `json.loads`, tolerate junk). On empty/error/malformed → **skip this item, increment `degraded`, NEVER emit a fallback junk claim**.
4. For each parsed claim object, validate the typed schema (§3); skip invalid ones.
5. `service.ingest(text=..., citations=[_citation(evidence, source_item)], idempotency_key=..., claim_type=<mapped>, subject=..., predicate=..., object_value=..., scope=scope, confidence=..., event_time=<ISO if present>, volatility="medium", source_agent="atlas-llm-extractor")`. Sensitivity filter + idempotency dedup apply automatically.
6. `dry_run=True` → do everything except `service.ingest` (return the drafted claims for inspection).

**Idempotency key:** `f"atlas-llm:evidence:{evidence.id}:{sha256(type|subject|predicate|object)[:16]}"`.

**Citation (FIX the hardcoded whatsapp):** provider-aware scheme — `gmail://`, `outlook://`, `gcal://`, `gdrive://`, `whatsapp://`, default `atlas://` — derived from `evidence.provider`/`source_item`. Keep `locator=evidence:{id}`, `excerpt=text[:500]`.

---

## 2. Prompt (the quality lever)

Instruct the model (cheap tier) to:
- Read the item (its provider, sender, date, body).
- Extract ONLY **durable, useful life-knowledge** about the user's world.
- **Emit `[]` (nothing)** for: newsletters, marketing, automated/bot notifications (GitHub/Vercel/PostHog/etc.), OTP/2FA, receipts with no obligation, pure FYI. (These are the exact false positives we observed.)
- For each real fact emit: `{type, subject, predicate, object, text, confidence, event_time?, relationship_to_user?}` where:
  - `type` ∈ person|company|project|product|topic|decision|commitment|preference|fact|event
  - `subject` = the **real entity** (a person/company/project name), NOT the source name
  - `text` = one self-contained sentence a future agent can act on
  - `event_time` = ISO-8601 when there's a date/deadline (esp. commitments/events)
  - `confidence` ∈ [0,1]
- Output a STRICT JSON array, no prose.
Include 2–3 few-shot examples in the prompt (one commitment, one person fact, one newsletter→`[]`).

---

## 3. Typed-claim validation + mapping

Validate each LLM claim object: required `type` (in the allowed set), `subject` (non-empty, not a bare source name like "Gmail"/"WhatsApp"), `text` (non-empty). Clamp `confidence` to [0,1] (default 0.6). `claim_type` = the `type` value (lowercased). Drop anything failing validation (count as skipped, not ingested).

---

## 4. CLI — `extract-atlas-claims` gains a mode

In `cli_handlers_*` / `cli.py`: add `--extractor {llm,deterministic}` (**default `llm`**), `--model`, `--dry-run` to the `extract-atlas-claims` subcommand. Dispatch: `llm` → `extract_atlas_claims_llm`; `deterministic` → existing `extract_atlas_claims_from_evidence` (preserved, unchanged). The LifeAgent bridge calls `extract-atlas-claims --scope X` unchanged and now gets the LLM path by default. If `llm` is selected but no LLM is usable, **degrade gracefully** (emit nothing + a clear note in the JSON result), do NOT silently fall back to the deterministic junk.

---

## 5. Tests (hermetic, MANDATORY — no network/LLM)

`tests/test_atlas_llm_extractor.py`: monkeypatch `atlas_llm_extractor.call_llm` to return canned JSON; seed a tmp `MemoryService` DB with synthetic evidence + source_items via `service.add_evidence_item`/`add_source_item`. Cover:
- **Typed extraction:** a WhatsApp body "te confirmo el pago el viernes" → a `commitment` claim with a real subject + `event_time`; an email body announcing a decision → a `decision` claim. Assert claim_type/subject/predicate are right.
- **Noise rejection (the core regression):** an email "from vercel[bot]: Re:[repo]" / a newsletter body → LLM returns `[]` → **0 claims ingested** (proves we don't reproduce the misclassification bug).
- **Graceful degrade:** `call_llm` returns "" / malformed JSON / raises → item skipped, `degraded` incremented, NO crash, NO junk claim.
- **Sensitivity routing:** an evidence body containing an API-key-shaped secret → the resulting claim is caught by the ingest filter (assert it's not stored verbatim / is flagged) — proves we still route through `service.ingest`.
- **Idempotency:** running twice ingests each claim once (stable key).
- **Citation is provider-aware:** a gmail-provider evidence → `gmail://` citation, not `whatsapp://`.
- **CLI dispatch:** `--extractor deterministic` still calls the old path; default calls the LLM path (monkeypatched).
- **`dry_run`** returns drafts without ingesting.

---

## 6. Acceptance criteria (verifiable)
- [ ] New extractor emits **typed entities with real subjects**, never `subject="whatsapp_contact"` or source-name subjects.
- [ ] Bot/newsletter fixtures → **0 claims** (the misclassification bug cannot recur — regression test).
- [ ] Malformed/empty LLM output → graceful skip, no crash, no junk.
- [ ] `extract-atlas-claims` defaults to `llm`; `--extractor deterministic` preserves old behavior (bridge contract intact).
- [ ] Every ingest goes through `service.ingest` (sensitivity filter proven by a test).
- [ ] Provider-aware citations (the hardcoded-`whatsapp://` bug fixed).
- [ ] New + existing tests green; ruff clean; full suite collects; CI green on the PR.

## 7. Out of scope (follow-up, private/runtime)
- Running the new extractor over the real 9,224 evidence items on the VM and merging the *good* claims into the brain (separate activation, personal data).
- Reviving the Jarvis planner (Ollama `gemma3n:e4b`) and restarting the import feed.
