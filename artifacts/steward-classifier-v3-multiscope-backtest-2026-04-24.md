# Steward classifier — v3old vs v3new back-test (item 11.5 multi-scope)

- db: `..\..\..\memorymaster.db`
- window: last 30 days
- extractor FEATURE_VERSION: `v3`
- v3old artifact: `artifacts\steward-classifier-v3-premultiscope-2026-04-24.joblib` (feature_version=`v3`, calibration=`sigmoid`, n_keys=22) — trained 2026-04-24T00:53 on single-scope `project:memorymaster` WikiCorpus
- v3new artifact: `artifacts\steward-classifier-v3.joblib` (feature_version=`v3`, calibration=`sigmoid`, n_keys=22) — trained 2026-04-24T14:31 on `scopes="*"` WikiCorpus (39 scopes, 272 articles)
- classifier threshold: `0.65` (+ citation >= 1)
- legacy thresholds: task-spec=`0.72` / live-prod=`0.58`

## Headline — ROC-AUC (training splits, train_steward_classifier.py)

| Split | v3old (single-scope) | v3new (multi-scope `*`) | Delta | Target | Verdict |
|-------|---------------------|-------------------------|-------|--------|---------|
| Sound (daily-stratified) | **0.9924** | **0.9895** | -0.0029 | ≥ 0.990 | **PASS** (stays above 0.99 bar) |
| Chronological | **0.5687** | **0.5778** | +0.0091 | > 0.45 strict / ≥ 0.60 stretch | **STRICT PASS, STRETCH MISS** (+0.009 toward 0.60, still 0.022 short) |

Note: the sound AUC drifts down 0.003 because the training-set size and label mix shift slightly (rebuilt in-process, 11496 rows vs the prior fixture's 11427). It stays well above the ≥ 0.990 acceptance bar.

## Headline — multi-scope aggregation (sanity check)

- scopes loaded: **39**
- articles loaded: **272**
- rows with non-zero `wiki_similarity_cosine`: **9356** / 11496 (81.4%)
- cross-project rows with non-zero `wiki_similarity_cosine` (scope != project:memorymaster): **9171** — these were all locked at 0 in v3old, so the multi-scope aggregation is live.

Top contributing scopes by article count:

| Scope | Articles |
|-------|----------|
| project:omniclaude | 69 |
| project:app | 22 |
| project:wezbridge | 17 |
| project:venezia | 16 |
| project:paperclip | 15 |
| project:memorymaster | 14 |
| project:_____testing | 14 |
| project:_omniclaude | 13 |
| global | 10 |
| project:testing | 10 |
| project:whatsappbot | 9 |
| project:final-inpla | 7 |
| project:interonda | 7 |
| project:omniremote | 6 |
| project:pages | 5 |
| project:pauol | 5 |
| project:puntofutura | 4 |
| project:guardar | 3 |
| project:testproject-landingpage | 3 |

(Plus 20 long-tail scopes with 1–2 articles each — full breakdown in `artifacts/steward-classifier-v3-multiscope-stats-2026-04-24.json`.)

Wiki backend: **tfidf** (MEMORYMASTER_DISABLE_ST=1 forced the deterministic path).

## Label-leakage disclosure

Both v2 and v3 artifacts were trained on the SAME DB we back-test on using a daily-stratified 80/20 split; claims created before `2026-04-09T00:00:00+00:00` were in the training corpus and carry label-leak risk. We report full-window AND out-of-sample-only metrics so readers can judge honestly.

- train-split overlap: **7646** rows (labels seen)
- test-split / out-of-sample: **3843** rows

## N events analyzed

- total: **11489**
- outcome=good (currently confirmed): 3679
- outcome=bad (archived/stale/superseded/conflicted): 7810
- outcome=unknown (still candidate): 0

## Confusion matrices — full 30-day window

### v3old @ 0.65

```
TP=3409  FP=336  TN=7474  FN=270  precision=0.9103  recall=0.9266  f1=0.9184
```

### v3new @ 0.65

```
TP=3530  FP=425  TN=7385  FN=149  precision=0.8925  recall=0.9595  f1=0.9248
```

### legacy @ 0.72 (task-spec Pareto)

```
TP=328  FP=113  TN=7697  FN=3351  precision=0.7438  recall=0.0892  f1=0.1592
```

### legacy @ 0.58 (live-prod)

```
TP=3493  FP=668  TN=7142  FN=186  precision=0.8395  recall=0.9494  f1=0.8911
```

## Confusion matrices — out-of-sample only

### v3old @ 0.65 — test split

```
TP=3329  FP=301  TN=115  FN=98  precision=0.9171  recall=0.9714  f1=0.9435
```

### v3new @ 0.65 — test split

```
TP=3419  FP=340  TN=76  FN=8  precision=0.9096  recall=0.9977  f1=0.9516
```

### legacy @ 0.58 — test split

```
TP=3255  FP=358  TN=58  FN=172  precision=0.9009  recall=0.9498  f1=0.9247
```

## Disagreement: v3old vs v3new

- only v3old promotes  (v2 says promote, v3 says archive): **13**  (of which good=5, bad=8)
- only v3new promotes  (v3 says promote, v2 says archive): **223**  (of which good=126, bad=97)
- both promote: **3732**
- both block: **7521**

### Sampled claims — only v3old promotes (v2=promote, v3=archive)

- **claim 8335** - status=`archived` outcome=`bad` source=`claude-session` type=`None` n_citations=1 in_train=False
  - v3old_proba=`0.660`  v3old_promote=`True`  v3new_proba=`0.632`  v3new_promote=`False`  legacy_score=`0.552`  legacy@0.72_promote=`False`  legacy@0.58_promote=`False`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 771.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.1083}`
  - text: When a user reports "scroll doesn't work" in a live-updating UI like a terminal mirror, there are at least 4 distinct root causes that present identically: (1) CSS overflow issue (scroll handler not engaging), (2) auto-...

- **claim 8323** - status=`archived` outcome=`bad` source=`claude-session` type=`filesystem_fact` n_citations=1 in_train=False
  - v3old_proba=`0.652`  v3old_promote=`True`  v3new_proba=`0.638`  v3new_promote=`False`  legacy_score=`0.552`  legacy@0.72_promote=`False`  legacy@0.58_promote=`False`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 589.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.073}`
  - text: Sticky-scroll pattern for live-updating terminal/chat panes (fixed in WezBridge SessionChat.js + TerminalView.js): the original useEffect did `el.scrollTop = el.scrollHeight` on every poll update, which made it impossib...

- **claim 8378** - status=`confirmed` outcome=`good` source=`claude-session` type=`gotcha` n_citations=1 in_train=False
  - v3old_proba=`0.705`  v3old_promote=`True`  v3new_proba=`0.627`  v3new_promote=`False`  legacy_score=`0.622`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 873.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0}`
  - text: invoicescanner gotcha — UUID existence oracle in API routes: when a route does `SELECT * WHERE id = $1` and then compares tenant_id in application code, returning 404 for not-found but 403 for foreign-owned creates a pr...

- **claim 8926** - status=`confirmed` outcome=`good` source=`claude-session` type=`filesystem_fact` n_citations=1 in_train=False
  - v3old_proba=`0.666`  v3old_promote=`True`  v3new_proba=`0.613`  v3new_promote=`False`  legacy_score=`0.588`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 1475.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0892}`
  - text: Multi-agent CLI detection patterns — COMPLETE set including working state captured 2026-04-12: CLAUDE CODE: - idle: ❯ prompt + "bypass permissions on" + "Ctx: XX%" status bar, title with ✳ - working: spinner glyph [✢✻✶✽...

- **claim 8173** - status=`archived` outcome=`bad` source=`claude-session` type=`None` n_citations=1 in_train=False
  - v3old_proba=`0.657`  v3old_promote=`True`  v3new_proba=`0.644`  v3new_promote=`False`  legacy_score=`0.552`  legacy@0.72_promote=`False`  legacy@0.58_promote=`False`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 432.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.1067}`
  - text: WezBridge completion events fired multiple times for the same session because Claude's background tasks cause terminal content to change slightly after "idle", triggering working→idle transitions repeatedly. The fix is ...

- **claim 8463** - status=`confirmed` outcome=`good` source=`claude-session` type=`gotcha` n_citations=1 in_train=False
  - v3old_proba=`0.708`  v3old_promote=`True`  v3new_proba=`0.629`  v3new_promote=`False`  legacy_score=`0.625`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 1039.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0}`
  - text: invoicescanner forward-compat pattern for unmigrated Supabase schemas (2026-04-10): when frontend code references columns added in a migration that hasn't been applied yet, Supabase PostgREST returns errors like "Could ...

- **claim 8170** - status=`archived` outcome=`bad` source=`claude-session` type=`None` n_citations=1 in_train=False
  - v3old_proba=`0.655`  v3old_promote=`True`  v3new_proba=`0.638`  v3new_promote=`False`  legacy_score=`0.552`  legacy@0.72_promote=`False`  legacy@0.58_promote=`False`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 448.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.1156}`
  - text: The WezBridge dashboard's chat parser (parseChatOutput) was too aggressive at filtering, returning 0 messages for idle sessions sitting at a bare ❯ prompt. The raw terminal output also contains status bar lines with emo...

- **claim 8274** - status=`stale` outcome=`bad` source=`claude-session` type=`filesystem_fact` n_citations=1 in_train=False
  - v3old_proba=`0.650`  v3old_promote=`True`  v3new_proba=`0.639`  v3new_promote=`False`  legacy_score=`0.552`  legacy@0.72_promote=`False`  legacy@0.58_promote=`False`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 853.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0729}`
  - text: WezBridge orchestrator bug (fixed): approving a spawn escalation via POST /api/orchestrator/escalations/:id/resolve was a silent no-op. Root cause: addEscalation() serialized the proposedAction into the markdown body as...

- **claim 8472** - status=`confirmed` outcome=`good` source=`claude-session` type=`gotcha` n_citations=1 in_train=False
  - v3old_proba=`0.710`  v3old_promote=`True`  v3new_proba=`0.648`  v3new_promote=`False`  legacy_score=`0.625`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 947.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0}`
  - text: invoicescanner schema mismatch gotcha: the LOCAL `dashboard-postgres` `invoices` table is MONOLITHIC — a single table with all AFIP fields inline (cae, numero_comprobante, importe_total, razon_social_emisor, cuit_emisor...

- **claim 8383** - status=`confirmed` outcome=`good` source=`claude-session` type=`gotcha` n_citations=1 in_train=False
  - v3old_proba=`0.706`  v3old_promote=`True`  v3new_proba=`0.642`  v3new_promote=`False`  legacy_score=`0.622`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 807.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0}`
  - text: invoicescanner external API cache thundering-herd fix: when a singleton module-level cache fronts a slow external API (ex: bluelytics full-history JSON, BCRA rates), two concurrent cold requests will both hit upstream, ...

### Sampled claims — only v3new promotes (v2=archive, v3=promote)

- **claim 9853** - status=`confirmed` outcome=`good` source=`llm-stop-hook` type=`fact` n_citations=1 in_train=False
  - v3old_proba=`0.618`  v3old_promote=`False`  v3new_proba=`0.987`  v3new_promote=`True`  legacy_score=`0.613`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.6, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 146.0, "has_verbatim_excerpt": 1.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.6732}`
  - text: The `spawn_session` MCP tool does not reliably trigger an 'enter' keypress after prompt injection, requiring an explicit `send_key('enter')` call.

- **claim 9833** - status=`confirmed` outcome=`good` source=`claude-session` type=`fact` n_citations=1 in_train=False
  - v3old_proba=`0.520`  v3old_promote=`False`  v3new_proba=`0.830`  v3new_promote=`True`  legacy_score=`0.629`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 658.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.4162}`
  - text: deploy/nginx/frontend.conf.template regex `location ~* ^/(assets|favicon.ico|manifest.json|robots.txt)$` shadowed the /assets SPA route because Vite's hashed-asset directory is named `assets/`. The exact `$`-anchored ma...

- **claim 8901** - status=`confirmed` outcome=`good` source=`omniclaude` type=`reference` n_citations=1 in_train=False
  - v3old_proba=`0.562`  v3old_promote=`False`  v3new_proba=`0.906`  v3new_promote=`True`  legacy_score=`0.721`  legacy@0.72_promote=`True`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.3, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 659.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.4359}`
  - text: OmniClaude watcher relaunch protocol worked correctly 2026-04-12 20:03 ART: watcher (task bpnpvnyd8) emitted "relaunch_me" event at ~55min mark signaling imminent timeout. Within seconds I re-launched Monitor with ident...

- **claim 8267** - status=`archived` outcome=`bad` source=`claude-session` type=`filesystem_fact` n_citations=1 in_train=False
  - v3old_proba=`0.474`  v3old_promote=`False`  v3new_proba=`0.668`  v3new_promote=`True`  legacy_score=`0.552`  legacy@0.72_promote=`False`  legacy@0.58_promote=`False`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 583.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.3094}`
  - text: When the orchestrator worker (Claude Code session in vault/_orchestrator-worker/) gets a state snapshot with both `missions` and `roadmap` populated, missions take strict priority over the global roadmap. The worker onl...

- **claim 7984** - status=`stale` outcome=`bad` source=`codex-session` type=`decision` n_citations=1 in_train=True
  - v3old_proba=`0.603`  v3old_promote=`False`  v3new_proba=`0.926`  v3new_promote=`True`  legacy_score=`0.621`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 218.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.695}`
  - text: The recommended first workflow layer build for personaldashboard is read-only: /work inbox, /projects pulse, and /registry backed by Clawtrol, CRM, Paperclip health, and BOOKMARKS ingestion before adding write actions.

- **claim 9866** - status=`confirmed` outcome=`good` source=`llm-stop-hook` type=`decision` n_citations=1 in_train=False
  - v3old_proba=`0.638`  v3old_promote=`False`  v3new_proba=`0.993`  v3new_promote=`True`  legacy_score=`0.613`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.6, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 104.0, "has_verbatim_excerpt": 1.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.81}`
  - text: Global CSS density optimizations yield diminishing returns compared to page-specific layout refactoring.

- **claim 8858** - status=`stale` outcome=`bad` source=`omniclaude` type=`fact` n_citations=1 in_train=False
  - v3old_proba=`0.466`  v3old_promote=`False`  v3new_proba=`0.793`  v3new_promote=`True`  legacy_score=`0.600`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.3, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 461.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.3687}`
  - text: OmniClaude session around 28h mark (2026-04-12 17:15 ART): session counters climbing across multiple panes — omniclaude 83%, memorymaster 88%, paperclip 90%, pane 20 whatsappbot 78%. Pane 20 still working on Paperclip r...

- **claim 8643** - status=`confirmed` outcome=`good` source=`omniclaude` type=`fact` n_citations=1 in_train=False
  - v3old_proba=`0.629`  v3old_promote=`False`  v3new_proba=`0.747`  v3new_promote=`True`  legacy_score=`0.719`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.3, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 480.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.1596}`
  - text: whatsappbot-final remediation program is officially CLOSED as of finaldive 2026-04-10. Bucket 1 (mandatory fixes) is empty. Remaining work is optional backlog: file decomposition (dashboardController.js, support.js, cus...

- **claim 8468** - status=`confirmed` outcome=`good` source=`claude-session` type=`gotcha` n_citations=1 in_train=False
  - v3old_proba=`0.635`  v3old_promote=`False`  v3new_proba=`0.895`  v3new_promote=`True`  legacy_score=`0.571`  legacy@0.72_promote=`False`  legacy@0.58_promote=`False`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 1.0, "n_citations": 1.0, "text_length": 646.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.3045}`
  - text: GOTCHA: Telegram "typing..." indicator is NOT a reliable diagnostic of Claude session state. server.ts (claude-plugins-official/telegram/0.0.4 line 908) calls `bot.api.sendChatAction(chat_id, 'typing')` unconditionally ...

- **claim 8677** - status=`stale` outcome=`bad` source=`omniclaude` type=`fact` n_citations=1 in_train=False
  - v3old_proba=`0.509`  v3old_promote=`False`  v3new_proba=`0.758`  v3new_promote=`True`  legacy_score=`0.594`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.3, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 450.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.2814}`
  - text: OmniClaude approaching 10h uptime (2026-04-11 13:22–23:28 ART), twelve Monitor relaunch cycles. Approaching midnight boundary — per CLAUDE.md circuit breakers, 00:00-09:00 ART is read_only mode (only alert for P0, no sp...

## v3old vs legacy disagreement

- v3old adds vs legacy@0.72: **3427** (good=3139, bad=288)
- v3old blocks vs legacy@0.72: **123**
- v3old adds vs legacy@0.58: **205** (good=166, bad=39)
- v3old blocks vs legacy@0.58: **621**

## v3new vs legacy disagreement

- v3new adds vs legacy@0.72: **3602** (good=3234, bad=368)
- v3new blocks vs legacy@0.72: **88**
- v3new adds vs legacy@0.58: **225** (good=183, bad=42)
- v3new blocks vs legacy@0.58: **431**

## Verdict

### Full 30-day window

- v3old: F1=0.9184 precision=0.9103 recall=0.9266
- v3new: F1=0.9248 precision=0.8925 recall=0.9595
- legacy@0.58 (live): F1=0.8911 precision=0.8395 recall=0.9494

### Out-of-sample only (honest)

- v3old: F1=0.9435 precision=0.9171 recall=0.9714
- v3new: F1=0.9516 precision=0.9096 recall=0.9977
- legacy@0.58 (live): F1=0.9247 precision=0.9009 recall=0.9498

**F1 gap on out-of-sample (v3new − v3old): +0.0081**
