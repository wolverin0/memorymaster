# Steward classifier — v2 vs v3 back-test

**Date:** 2026-04-24
**Branch:** `omni/feat-steward-v3-2026-04-23`
**New feature in v3:** `wiki_similarity_cosine` — cosine similarity between
claim `(subject + text)` and the best-matching wiki article's
compiled-truth body, computed via TF-IDF (char-4gram) fallback when
`sentence-transformers` is disabled. Article selection honours the
claim's explicit `wiki_article` column first, else token-overlap picks
the best slug in scope.

## Headline — acceptance criteria

| Metric | v2 | v3 | v3 acceptance target | Pass? |
|---|---:|---:|---|:--:|
| ROC-AUC — sound (daily-stratified) | 0.9898 | **0.9924** | ≥ 0.990 | ✅ |
| ROC-AUC — chronological 80/20 | 0.45 (known pathology) | **0.5687** | strictly > 0.45; stretch ≥ 0.60 | ✅ (strict), near stretch |

> The chronological-split pathology was documented in
> `artifacts/steward-classifier-feature-audit-2026-04-23.md`: the most
> recent 20% of the corpus is ~94% positive because archive sweeps lag
> behind confirmations. The goal for v3 was *any* real lift on that split
> (0.5687 vs 0.45 = **+0.12 ROC-AUC**). Stretch target of 0.60 was almost
> reached (0.031 short). Honest null result is still a win vs v2.
>
> Calibration: `CalibratedClassifierCV(LogisticRegression(C=0.5,
> class_weight='balanced', random_state=42, solver='lbfgs',
> max_iter=5000), method='sigmoid', cv=3)` — per the v3 spec.

## Run metadata

- db: `..\..\..\memorymaster.db` (read-only, WAL replay on, no writes)
- window: last 30 days
- extractor FEATURE_VERSION: `v3`
- v2 artifact: `artifacts\steward-classifier-v2.joblib` (feature_version=`v2`, calibration=`unknown`, n_keys=21)
- v3 artifact: `artifacts\steward-classifier-v3.joblib` (feature_version=`v3`, calibration=`sigmoid`, n_keys=22)
- classifier threshold: `0.65` (+ citation >= 1)
- legacy thresholds: task-spec=`0.72` / live-prod=`0.58`
- random_state: 42 (LogisticRegression); sampling seed: 1337

## Training numbers (from `scripts/train_steward_classifier.py --version v3`)

```
[train] version=v3 calibration=sigmoid
[train] rows=11427 primary_split=daily-stratified train=9128 test=2299
[train] test class counts: pos=786 neg=1513
[train] ROC-AUC (daily-stratified)=0.9924
[train] ROC-AUC (chronological)=0.5687 (train=9141 test=2286)
[train] @threshold=0.65: precision=0.9732 recall=0.9237
```

## `wiki_similarity_cosine` distribution on the training fixture

- corpus: 9 articles under `obsidian-vault/wiki/project-memorymaster/`
- backend: TF-IDF char-(3,5)-gram (deterministic, no external downloads)
- claims with non-zero similarity: 10290 / 11427 (90.1%)
- distribution: min=0.0000  median=0.1248  mean=0.1253  max=0.6373

Three representative samples across the distribution:

**HIGH (~0.63)** — claim 11776, `wiki_article='memorymaster-setup-hooks'`:
> "Installed ~/.claude/hooks/memorymaster-auto-ingest.py has drifted
> into a block-based auto-save rewrite ..."

The claim *is about* a topic that has a compiled-truth wiki article.
Strong lexical overlap with the article's body. The high similarity
signals the classifier that this claim is "grounded" in an existing
compiled truth.

**MEDIUM (~0.23)** — claim 6959, `wiki_article=None`:
> "Lessons Learned — Middleware pattern perfect fit for pre-routing
> checks ..."

Token-overlap fallback picked a generic article. The claim has mild
shared vocabulary with one of the wiki articles — probably `workflow`
or `general`.

**LOW (~0.011)** — claim 9293, `wiki_article='general'`:
> "Dude clone migration test gotcha: backend/migrations/embed_test.go
> hardcodes expected migration count..."

A grounded claim whose article ("general") is so generic that only a
handful of character trigrams overlap. Still non-zero, still useful as
a weak "this claim has a home" signal.

**ZERO** (1137 claims): claims whose text shares no char-ngrams with
any wiki article compiled-truth — either the scope has no matching
article or the claim is cross-scope (e.g., `project:unrelated`).

## Label-leakage disclosure

Both v2 and v3 artifacts were trained on the SAME DB we back-test on using a daily-stratified 80/20 split; claims created before `2026-04-09T00:00:00+00:00` were in the training corpus and carry label-leak risk. We report full-window AND out-of-sample-only metrics so readers can judge honestly.

- train-split overlap: **7635** rows (labels seen)
- test-split / out-of-sample: **3767** rows

## N events analyzed

- total: **11402**
- outcome=good (currently confirmed): 3702
- outcome=bad (archived/stale/superseded/conflicted): 7700
- outcome=unknown (still candidate): 0

## Confusion matrices — full 30-day window

### v2 @ 0.65

```
TP=3534  FP=345  TN=7355  FN=168  precision=0.9111  recall=0.9546  f1=0.9323
```

### v3 @ 0.65

```
TP=3500  FP=314  TN=7386  FN=202  precision=0.9177  recall=0.9454  f1=0.9313
```

### legacy @ 0.72 (task-spec Pareto)

```
TP=357  FP=110  TN=7590  FN=3345  precision=0.7645  recall=0.0964  f1=0.1713
```

### legacy @ 0.58 (live-prod)

```
TP=3587  FP=620  TN=7080  FN=115  precision=0.8526  recall=0.9689  f1=0.9071
```

## Confusion matrices — out-of-sample only

### v2 @ 0.65 — test split

```
TP=3400  FP=301  TN=57  FN=9  precision=0.9187  recall=0.9974  f1=0.9564
```

### v3 @ 0.65 — test split

```
TP=3373  FP=299  TN=59  FN=36  precision=0.9186  recall=0.9894  f1=0.9527
```

### legacy @ 0.58 — test split

```
TP=3308  FP=351  TN=7  FN=101  precision=0.9041  recall=0.9704  f1=0.936
```

## Disagreement: v2 vs v3

- only v2 promotes  (v2 says promote, v3 says archive): **77**  (of which good=41, bad=36)
- only v3 promotes  (v3 says promote, v2 says archive): **12**  (of which good=7, bad=5)
- both promote: **3802**
- both block: **7511**

### Sampled claims — only v2 promotes (v2=promote, v3=archive)

- **claim 9060** - status=`confirmed` outcome=`good` source=`omniclaude` type=`project` n_citations=1 in_train=False
  - v2_proba=`0.695`  v2_promote=`True`  v3_proba=`0.636`  v3_promote=`False`  legacy_score=`0.680`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.3, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 907.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.2475}`
  - text: whatsappbot overnight major findings 2026-04-13 04:00-04:30 ART (pane 20): (1) PR #39 opened restoring AutoBajaService — 69/69 eligible BAJAs created and assigned to Ruben for uninstall. (2) UISP payment duplicate scan ...

- **claim 8191** - status=`confirmed` outcome=`good` source=`claude-session` type=`filesystem_fact` n_citations=1 in_train=False
  - v2_proba=`0.687`  v2_promote=`True`  v3_proba=`0.624`  v3_promote=`False`  legacy_score=`0.720`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 1.0, "n_citations": 1.0, "text_length": 1293.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.1788}`
  - text: Paperclip agent deletion gotcha: the DELETE /api/agents/{id} endpoint fails with HTTP 500 for any agent that has heartbeat_runs, issues assignments, activity_log entries, cost_events, or other FK dependencies — which is...

- **claim 9285** - status=`confirmed` outcome=`good` source=`clawfleet-pane10` type=`decision` n_citations=1 in_train=False
  - v2_proba=`0.788`  v2_promote=`True`  v3_proba=`0.587`  v3_promote=`False`  legacy_score=`0.725`  legacy@0.72_promote=`True`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.3, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 1937.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.2769}`
  - text: clawfleet Phase 3 night-mode run 2026-04-14 04:00-04:10 ART: shipped 5 feature branches on top of the v1.0 + dashboard-mvp base, all pushed to github.com/wolverin0/clawfleet as reviewable branches (NOT merged to main — ...

- **claim 8039** - status=`confirmed` outcome=`good` source=`claude-session` type=`fact` n_citations=1 in_train=True
  - v2_proba=`0.918`  v2_promote=`True`  v3_proba=`0.631`  v3_promote=`False`  legacy_score=`0.641`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 284.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.5104}`
  - text: LongMemEval benchmark with FTS5 keyword search scores ~10% on 20 questions. Main issues: FTS5 misses semantic matches (different words same meaning), temporal reasoning fails because claims lack ordering context. Need v...

- **claim 902** - status=`stale` outcome=`bad` source=`None` type=`fact` n_citations=1 in_train=True
  - v2_proba=`0.924`  v2_promote=`True`  v3_proba=`0.314`  v3_promote=`False`  legacy_score=`0.532`  legacy@0.72_promote=`False`  legacy@0.58_promote=`False`
  - features: `{"source_agent_trust": 0.1, "scope_quality": 0.4, "n_citations": 1.0, "text_length": 510.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.1744}`
  - text: 🌐 Conectividad: Acceso Externo a Archivos (Update 13:02)\n- **Hito**: Se habilitó el acceso externo al visor de archivos vía `https://view.puntofutura.com.ar` bypassando Cloudflare Access.\n- **Acciones Técnicas**:\n - ...

- **claim 8174** - status=`confirmed` outcome=`good` source=`claude-session` type=`filesystem_fact` n_citations=1 in_train=False
  - v2_proba=`0.661`  v2_promote=`True`  v3_proba=`0.644`  v3_promote=`False`  legacy_score=`0.560`  legacy@0.72_promote=`False`  legacy@0.58_promote=`False`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 337.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0515}`
  - text: To run the wezbridge dashboard: cd to the project and use `npm run dev` (node --watch for auto-restart on code changes), or `npm run dashboard` (with --open to auto-open browser), or `npm run dashboard:persistent` (wrap...

- **claim 9359** - status=`confirmed` outcome=`good` source=`clawfleet-pane10` type=`decision` n_citations=1 in_train=False
  - v2_proba=`0.833`  v2_promote=`True`  v3_proba=`0.501`  v3_promote=`False`  legacy_score=`0.730`  legacy@0.72_promote=`True`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.3, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 1668.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.3477}`
  - text: clawfleet merge pass v1.1→v1.5 complete 2026-04-14 (task T-021): all 11 feature branches merged into main at github.com/wolverin0/clawfleet. Final state: HEAD=ff6ac8e. Tags: v1.1.0 (dashboard-mvp, 1 branch, 0 conflicts)...

- **claim 445** - status=`stale` outcome=`bad` source=`None` type=`fact` n_citations=1 in_train=True
  - v2_proba=`0.991`  v2_promote=`True`  v3_proba=`0.202`  v3_promote=`False`  legacy_score=`0.536`  legacy@0.72_promote=`False`  legacy@0.58_promote=`False`
  - features: `{"source_agent_trust": 0.1, "scope_quality": 0.4, "n_citations": 1.0, "text_length": 200.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.105}`
  - text: Script: `scripts/session_to_daily_note.py`\n- Lee session JSONLs del día actual\n- Filtra mensajes de usuario (excluye cron, heartbeat, automated)\n- Genera un daily note estructurado con: temas, deci

- **claim 8307** - status=`confirmed` outcome=`good` source=`claude-session` type=`filesystem_fact` n_citations=1 in_train=False
  - v2_proba=`0.727`  v2_promote=`True`  v3_proba=`0.609`  v3_promote=`False`  legacy_score=`0.737`  legacy@0.72_promote=`True`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 1.0, "n_citations": 1.0, "text_length": 1062.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.2792}`
  - text: CRITICAL feedback on verification method: grepping server-side rendered HTML for text strings is NOT a valid test that a Next.js page actually renders in a browser. Demonstrated on 2026-04-09: rebuilt resell storefront ...

- **claim 382** - status=`stale` outcome=`bad` source=`None` type=`fact` n_citations=1 in_train=True
  - v2_proba=`0.962`  v2_promote=`True`  v3_proba=`0.283`  v3_promote=`False`  legacy_score=`0.536`  legacy@0.72_promote=`False`  legacy@0.58_promote=`False`
  - features: `{"source_agent_trust": 0.1, "scope_quality": 0.4, "n_citations": 1.0, "text_length": 539.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.1262}`
  - text: Tasks ClawTrol ejecutadas - **#707 ReadFile: config.yaml** — Loopeo múltiple (AUTO-PULL repetido). Causa: task nunca cerraba. Ejecutado manualmente: `config.yaml` no existe, OpenClaw usa `openclaw.json`. Cerrado con `ag...

### Sampled claims — only v3 promotes (v2=archive, v3=promote)

- **claim 8771** - status=`stale` outcome=`bad` source=`omniclaude` type=`fact` n_citations=1 in_train=False
  - v2_proba=`0.592`  v2_promote=`False`  v3_proba=`0.720`  v3_promote=`True`  legacy_score=`0.721`  legacy@0.72_promote=`True`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.3, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 781.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0}`
  - text: OmniClaude cross-agent orchestration test successful through 6 rounds (2026-04-12 05:21–06:27 ART): pane 20 (designer, whatsappbot-final) ↔ pane 21 (implementer, paperclip-agent-consolidation). Test validated: (1) manua...

- **claim 7546** - status=`confirmed` outcome=`good` source=`None` type=`fact` n_citations=2 in_train=True
  - v2_proba=`0.407`  v2_promote=`False`  v3_proba=`0.693`  v3_promote=`True`  legacy_score=`0.849`  legacy@0.72_promote=`True`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.1, "scope_quality": 0.8, "n_citations": 2.0, "text_length": 173.0, "has_verbatim_excerpt": 1.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0}`
  - text: OmniRemote Alert hostname comes from selectinload(Alert.device) relationship in alerts.py. Frontend also cross-references device list as fallback via useDeviceHostnames hook

- **claim 7494** - status=`confirmed` outcome=`good` source=`None` type=`fact` n_citations=2 in_train=True
  - v2_proba=`0.628`  v2_promote=`False`  v3_proba=`0.756`  v3_promote=`True`  legacy_score=`0.726`  legacy@0.72_promote=`True`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.1, "scope_quality": 0.8, "n_citations": 2.0, "text_length": 189.0, "has_verbatim_excerpt": 1.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0}`
  - text: Interonda invoice client search had a blocking mismatch fixed by filtering estado_servicio with lowercase 'activo' instead of 'Activo', matching the rest of the client status normalization.

- **claim 7824** - status=`archived` outcome=`bad` source=`None` type=`decision` n_citations=1 in_train=True
  - v2_proba=`0.569`  v2_promote=`False`  v3_proba=`0.727`  v3_promote=`True`  legacy_score=`0.620`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.1, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 160.0, "has_verbatim_excerpt": 1.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0}`
  - text: The embedded local portal now talks only to agent-local /app/* broker endpoints, and the agent proxies to control-plane /api/agent/* routes using AGENT_API_KEY.

- **claim 7818** - status=`archived` outcome=`bad` source=`None` type=`decision` n_citations=1 in_train=True
  - v2_proba=`0.569`  v2_promote=`False`  v3_proba=`0.725`  v3_promote=`True`  legacy_score=`0.620`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.1, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 179.0, "has_verbatim_excerpt": 1.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0}`
  - text: The embedded agent local portal must talk to the control plane through agent-brokered machine-to-machine endpoints, not direct browser calls to JWT-protected control-plane routes.

- **claim 7944** - status=`archived` outcome=`bad` source=`codex-session` type=`fact` n_citations=1 in_train=True
  - v2_proba=`0.532`  v2_promote=`False`  v3_proba=`0.656`  v3_promote=`True`  legacy_score=`0.624`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 1.0, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 174.0, "has_verbatim_excerpt": 0.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0}`
  - text: In gimnasio-next auth reset-token helpers, using the concrete Drizzle db type avoids Vercel production typecheck failures caused by overly narrow structural typing of ctx.db.

- **claim 7607** - status=`archived` outcome=`bad` source=`None` type=`constraint` n_citations=1 in_train=True
  - v2_proba=`0.546`  v2_promote=`False`  v3_proba=`0.654`  v3_promote=`True`  legacy_score=`0.624`  legacy@0.72_promote=`False`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.1, "scope_quality": 0.8, "n_citations": 1.0, "text_length": 179.0, "has_verbatim_excerpt": 1.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0866}`
  - text: Askey RTF3505VW (movi4011) hangs every 12-24h. Load avg 10+, firmware V2.57 old. Auto-reboot cron at 4:30AM exists but daytime crashes not covered. WAN mangle auto-disable is OFF.

- **claim 7495** - status=`confirmed` outcome=`good` source=`None` type=`fact` n_citations=2 in_train=True
  - v2_proba=`0.594`  v2_promote=`False`  v3_proba=`0.701`  v3_promote=`True`  legacy_score=`0.731`  legacy@0.72_promote=`True`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.1, "scope_quality": 0.8, "n_citations": 2.0, "text_length": 194.0, "has_verbatim_excerpt": 1.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0862}`
  - text: Interonda invoice backoffice UI now includes a per-invoice Enviar action in /billing/invoices with loading and error feedback using useSendInvoiceBackoffice and a toast/banner confirmation path.

- **claim 7505** - status=`confirmed` outcome=`good` source=`None` type=`decision` n_citations=2 in_train=True
  - v2_proba=`0.407`  v2_promote=`False`  v3_proba=`0.699`  v3_promote=`True`  legacy_score=`0.844`  legacy@0.72_promote=`True`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.1, "scope_quality": 0.8, "n_citations": 2.0, "text_length": 174.0, "has_verbatim_excerpt": 1.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0}`
  - text: Fase 6 reportes legacy de compras se modelan como un catalogo puro sobre proveedores, facturas de proveedores, ordenes de pago y pagos, con navegacion operativa a /suppliers.

- **claim 7504** - status=`confirmed` outcome=`good` source=`None` type=`decision` n_citations=2 in_train=True
  - v2_proba=`0.407`  v2_promote=`False`  v3_proba=`0.697`  v3_promote=`True`  legacy_score=`0.735`  legacy@0.72_promote=`True`  legacy@0.58_promote=`True`
  - features: `{"source_agent_trust": 0.1, "scope_quality": 0.8, "n_citations": 2.0, "text_length": 183.0, "has_verbatim_excerpt": 1.0, "n_related_claims": 0.0, "conflict_delta": 0.0, "wiki_similarity_cosine": 0.0}`
  - text: Fase 6 accounting report catalog was implemented as a pure catalog over local payments plus supplier liabilities, with presets routing to /payments, /billing/invoices, and /suppliers.

## v2 vs legacy disagreement

- v2 adds vs legacy@0.72: **3512** (good=3224, bad=288)
- v2 blocks vs legacy@0.72: **100**
- v2 adds vs legacy@0.58: **143** (good=109, bad=34)
- v2 blocks vs legacy@0.58: **471**

## v3 vs legacy disagreement

- v3 adds vs legacy@0.72: **3458** (good=3199, bad=259)
- v3 blocks vs legacy@0.72: **111**
- v3 adds vs legacy@0.58: **112** (good=105, bad=7)
- v3 blocks vs legacy@0.58: **505**

## Verdict

### Full 30-day window

- v2: F1=0.9323 precision=0.9111 recall=0.9546
- v3: F1=0.9313 precision=0.9177 recall=0.9454
- legacy@0.58 (live): F1=0.9071 precision=0.8526 recall=0.9689

### Out-of-sample only (honest)

- v2: F1=0.9564 precision=0.9187 recall=0.9974
- v3: F1=0.9527 precision=0.9186 recall=0.9894
- legacy@0.58 (live): F1=0.936 precision=0.9041 recall=0.9704

**F1 gap on out-of-sample (v3 − v2): -0.0037**
