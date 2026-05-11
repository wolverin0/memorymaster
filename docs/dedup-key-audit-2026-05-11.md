# Dedup Key Prefix-Collision Audit - 2026-05-11

## Scope

Audited `memorymaster/` and `scripts/` for patterns similar to PR #31, where
`verbatim_store.search_verbatim` previously deduped hybrid search rows using
`content[:100]`. PR #31 is verified at:

- PR: https://github.com/wolverin0/memorymaster/pull/31
- Merge commit: `80e4eb659ca3d0e80bd704cc400fddd956a5fa1a`
- Reported live data in PR body: 4,258 colliding 100-char prefixes, worst case
  25,894 rows on one prefix.

## Methodology

- Attempted local shell commands first for `git checkout`, `git branch`,
  `rg`, `gh pr view`, `git log`, tests, commits, and push. The shell sandbox
  failed before process launch with `windows sandbox: spawn setup refresh`.
- Used MemoryMaster `query_memory` for prior context; no relevant claims found.
- Used GitNexus for indexed symbol context and impact where symbols were
  inspectable. GitNexus reported the index was one commit stale, so current
  source checks used GitHub file fetches from `wolverin0/memorymaster@main`.
- Used GitHub connector checks for PR #31, PR URL, merge commit, and current
  file contents. GitHub code search returned no results for literal pattern
  queries, so targeted file fetches were used around the dedup/search/retrieval,
  MCP, hook, storage, and recall-eval code paths.

## Findings

| Site | Pattern | Risk | Reasoning | Proposed fix |
|---|---|---:|---|---|
| `memorymaster/verbatim_store.py:335` | `texts = [r["content"][:2000] for r in rows]` | MEDIUM | This truncates text before embedding. It is not a dedup key by itself, but long templated rows with identical first 2000 chars get identical embedding input. | Keep if this is only an embedding-token cap; otherwise include a separate full-content hash in the vector payload. |
| `memorymaster/verbatim_store.py:352` | Qdrant payload stores `"content": row["content"][:2000]` | HIGH | Vector results do not include the Qdrant point id in `_search_vector`; PR #31 fallback hashes the returned payload content. If two same-session rows share the first 2000 chars, hybrid merge can still collapse them. This file was touched by PR #31, so no edit was made under the "do not touch PR #31-#38 files" rule. | Return `id: h.get("id")` from `_search_vector`, or store `content_hash=sha256(full content)` in payload and key on `(session_id, content_hash)`. |
| `memorymaster/_storage_shared.py:26` | `hashlib.sha256(text).hexdigest()[:4]` for human IDs | LOW | Hashes full text/subject, then truncates the digest. It is not used as a dedup key; collisions affect display ID readability, not claim uniqueness. | No urgent fix. If human ID ambiguity becomes operationally painful, expand to 8+ hex chars. |
| `memorymaster/mcp_server.py` `_project_scope` | `sha1(workspace_path).hexdigest()[:8]` for optional scope disambiguation | LOW | Hashes the full workspace path and uses an 8-hex digest only when `MEMORYMASTER_SCOPE_DISAMBIGUATE=1`. Not templated text and not a dedup key. | Keep. If many same-slug workspaces exist on one host, expand to 12+ hex chars. |
| `memorymaster/mcp_server.py` `ingest_claim` timeline write | `claim.text[:200]` as timeline summary | LOW | Display summary only; not a key and does not dedup. | Keep, or rename variable/comment to make non-key intent obvious. |
| `memorymaster/mcp_server.py` `_apply_detail_level` | `(claim_dict.get("text") or "")[:80]` for summary response | LOW | Response projection only; not a key and does not dedup. | Keep. |
| `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py` | `messages.append(text[:500])` | LOW | LLM context limiting only; not a dedup key. Repeated assistant prefixes can lose suffix detail, but this affects extraction context, not row uniqueness. | Keep, or prefer token budgeting if extraction quality suffers. |
| `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py` | `sha256(text.strip().lower()).hexdigest()[:16]` in `llm-stop-*` idempotency key | LOW | Hashes the full normalized claim text, then truncates to 64 bits. This is collision-resistant for hook-scale data and is not prefix-derived. | Consider full digest if idempotency-key collisions are ever observed. |
| `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py` | `text[:200]` citation excerpt | LOW | Citation excerpt only; not a key and does not dedup. | Keep. |
| `scripts/expand_recall_eval.py:71` | `sha1(text).hexdigest()[:16]` prompt label key | LOW | Hashes full prompt text; 64-bit digest prefix is used as an eval-label key. Not prefix-derived from user text. | Keep, or use full SHA1 in artifacts if label collision paranoia matters. |
| `scripts/expand_recall_eval.py:204-206` | `seen_exact` set uses normalized full `rec.text` | LOW | This is a dedup set, but the key is full normalized prompt text, not a prefix. Near-dup detection uses token Jaccard, not string prefix. | Keep. |
| `scripts/eval_recall_precision_at_5.py:115` | `sha1(text).hexdigest()[:16]` prompt label key | LOW | Matches `expand_recall_eval._sha`; hashes the full prompt and truncates the digest. Not prefix-derived from user text. | Keep, or migrate both scripts to full SHA1 together. |
| `scripts/eval_recall_precision_at_5.py` `_evaluate_prompt` | `prompt=rec.text[:120]` in eval output | LOW | Output preview only; not a key and does not dedup. | Keep. |

## Classification Summary

- HIGH: 1 site
- MEDIUM: 1 site
- LOW: 11 sites

No source fix was applied because the only HIGH-risk site is in
`memorymaster/verbatim_store.py`, which is explicitly covered by recently merged
PR #31 and listed in the "do not touch" set for this task. The minimum viable
future fix is to include the Qdrant point id in `_search_vector` results so
`_row_dedup_key` can use the already preferred `("id", rid)` branch.

## Verification Status

- PR #31 metadata verified with GitHub connector.
- Merge commit `80e4eb659ca3d0e80bd704cc400fddd956a5fa1a` fetched and inspected.
- GitNexus impact checked for `_row_dedup_key` and `sync_to_qdrant`; both were
  LOW blast radius in the stale index.
- Local `rg`, tests, commits, push, `gh pr view`, and `git log` could not be run
  because no shell process could start in this environment.
