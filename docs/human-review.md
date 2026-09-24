<!-- doc-head: optional human judgments after agent-owned technical memory review -->
Covers: reading evidence, saving local drafts, final judgments and review export.
Read when: opening a prepared review.html or using the dashboard /review page.
Human judgments evaluate memory quality; this page does not promote or change claims.
<!-- /doc-head -->

# Review memories

The agent first checks evidence, current documentation, contradictions, scope,
duplicates and marginal usefulness. Do not use this page to make the operator
perform that technical review. Human input is for preferences or business facts
the agent cannot establish. AI judgments remain labeled AI.

The September v1 sample was technically re-reviewed: none of the 13 statements
was accepted as emitted. See `AI-REREVIEW-13.md` in the E2E audit directory. The
page remains a historical evaluation surface, not a recommended-memory list.

Open the prepared `review.html` in your browser. It already contains the review
sample and does not need a running MemoryMaster service. Alternatively, open
`/review` in a dashboard running this candidate and load its `packet.json`.

1. Enter your reviewer name.
2. Read the memory and its quoted source passage. Expand the full source message
   when you need more context.
3. Answer whether the source supports it, whether it is worth remembering,
   whether it is still current, and whether it belongs in the displayed scope.
   If the scope is wrong, supply a different corrected scope.
4. Choose Accept, Reject or Unsure, and explain your decision briefly.
5. Use Previous/Next or the filter to continue. Download **Export complete JSONL**
   when ready, then provide that file for evaluation.

Any unanswered or Unsure choice keeps a record as a draft. Only records with all
four questions answered Yes/No, an Accept/Reject decision, a reviewer name and a
non-empty explanation are exported as finalized reviews. The page shows how many
records will be excluded. Reject is a completed judgment; Unsure is not.

Progress is saved in this browser when local storage is available. Reopen the same
HTML file to continue. On the dashboard page, load the same packet again to restore
its saved judgments. Source passages are not copied into local storage. The page
shows a warning if local saving is unavailable; download your work before closing.
**Export drafts JSON** saves unfinished judgments as a backup for later assistance;
it is not an acceptance file and is not imported by the evaluator.

The preliminary v1 sample has 13 emitted memories. Reviewing all 13 is useful
feedback, but does not satisfy the required 20 human reviews. A sufficiently large
later cohort and the existing quality thresholds are still required. Downloading
reviews does not apply, promote, retire or otherwise mutate any memory.

## Preparing a page from frozen inputs

Use a new output directory; the command refuses to overwrite an existing one:

```powershell
python scripts/prepare_dreaming_review.py --cohort $FrozenCohort --ai-labels $AiLabels --output $NewReviewDirectory
```

The command checks the frozen source fingerprint, decision identifiers and label
coverage, then writes `packet.json` and `review.html`. The HTML contains local
evidence, so handle it with the same care as the original cohort. No external
scripts or data requests are required by the review page.

Final JSONL is checked against its packet and passed to the existing cohort
evaluator. AI preparation and human judgments remain separate; a file download is
not proof that the quality gate passed.
