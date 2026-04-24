# Wiki freshness baseline — 2026-04-24 (roadmap 11.8, Option A)

## Summary

- **Vault scanned:** `obsidian-vault/wiki/`
- **Total articles:** 275 (across 39 scopes)
- **Scan time:** ~22–77 ms wall-clock on the full vault (pure filesystem, no DB)
- **Metric:** `freshness_score = exp(-days_since_last_absorb / 30)`
- **Truth source:** article frontmatter `date:` field, fallback to file mtime
- **Shape:** Option A only — single-signal (absorb recency). Composite with B/C/D deferred.

## Distribution

| Bucket | Score range | Count | Share |
|---|---|---:|---:|
| fresh | ≥ 0.5 | 275 | 100% |
| mid | 0.2 – 0.5 | 0 | 0% |
| stale | < 0.2 | 0 | 0% |

The entire live vault was absorbed within the last ~18 days, so no article
currently crosses the stale threshold. `lint-vault`'s new `STALE_ARTICLE`
check therefore reports **0 hits** on the baseline.

### Histogram (score buckets)

```
0.0 – 0.1:   0
0.1 – 0.2:   0
0.2 – 0.3:   0
0.3 – 0.4:   0
0.4 – 0.5:   0
0.5 – 0.6:   1  #
0.6 – 0.7:  10  ##########
0.7 – 0.8:   6  ######
0.8 – 0.9:  32  ################################
0.9 – 1.0: 226  ########################################################################
```

## Top 20 stalest articles

| freshness | days | scope | title |
|---:|---:|---|---|
| 0.5518 | 17.8 | project-pedrito | General |
| 0.6147 | 14.6 | wiki | Wiki Resolver |
| 0.6355 | 13.6 | project-memorymaster | Qdrant |
| 0.6355 | 13.6 | project-paperclip | Wispbot Usersessions |
| 0.6355 | 13.6 | project-whatsappbot | Wispbot |
| 0.6571 | 12.6 | project-puntofutura | Demo Pipeline |
| 0.6794 | 11.6 | project-impulsa | Impulsa |
| 0.6794 | 11.6 | project-interonda | Interonda Demo Dataset |
| 0.6794 | 11.6 | project-interonda | Interonda |
| 0.6794 | 11.6 | project-whatsapp-bot | Auth |
| 0.6794 | 11.6 | project-whatsappbot-prod---copy---copy | General |
| 0.7024 | 10.6 | project-memorymaster | General |
| 0.7048 | 10.5 | wiki | log |
| 0.7508 | 8.6 | project-interonda | Whatsappbot-Final Audit Remediation |
| 0.7508 | 8.6 | project-paperclip | Qdrant |
| 0.7762 | 7.6 | project-app | Auth |
| 0.7762 | 7.6 | project-whatsappbot | Paperclip |
| 0.8026 | 6.6 | project-clawcode | Claw-Code |
| 0.8026 | 6.6 | project-paperclip | Autobajaservice |
| 0.8298 | 5.6 | project-final-inpla | Final-Inpla Clean Export |

## Top 20 freshest articles (sample)

All 226 articles with score > 0.9 have `days_since_absorb` = 0.6 (today's
absorb run). Showing one scope sample per line:

| freshness | days | scope | title |
|---:|---:|---|---|
| 0.9803 | 0.6 | user | Workspace |
| 0.9803 | 0.6 | project-wificsi | Wiflow Ground Truth Collection |
| 0.9803 | 0.6 | project-whatsappbot | Uisp |
| 0.9803 | 0.6 | project-whatsappbot | Testbotdux Enrichment Pipeline |
| 0.9803 | 0.6 | project-whatsappbot | Incident-Engine |
| 0.9803 | 0.6 | project-whatsappbot | Evolution-Api |
| 0.9803 | 0.6 | project-wezbridge | @Wterm/React |
| 0.9803 | 0.6 | project-wezbridge | Wezbridge |
| 0.9803 | 0.6 | project-wezbridge | V3.0 Architecture |
| 0.9803 | 0.6 | project-wezbridge | Omniclaude-Rollout-Plan |
| 0.9803 | 0.6 | project-wezbridge | Coordinator |
| 0.9803 | 0.6 | project-wezbridge | Claude.Md |
| 0.9803 | 0.6 | project-wezbridge | Claude Code |
| 0.9803 | 0.6 | project-wezbridge | Agent-Browser |
| 0.9803 | 0.6 | project-wezbridge | A2A Protocol |
| 0.9803 | 0.6 | project-wezbridge | A-6 Observer |
| 0.9803 | 0.6 | project-venezia | Venezia |
| 0.9803 | 0.6 | project-venezia | Venezia-Local-Stack |
| 0.9803 | 0.6 | project-venezia | Supabase |
| 0.9803 | 0.6 | project-venezia | Sidebar |

## CLI usage

```bash
# Full sorted table (stalest first)
python -m memorymaster wiki-freshness

# Custom vault path
python -m memorymaster wiki-freshness --vault obsidian-vault/wiki

# Filter to articles below a freshness score
python -m memorymaster wiki-freshness --below 0.4

# Filter by absolute day threshold (converted to equivalent score internally)
python -m memorymaster wiki-freshness --threshold-days 90

# Machine-readable (wrapped in the standard {ok, data, meta} envelope)
python -m memorymaster --json wiki-freshness
```

## Implementation notes

- Code lives in `memorymaster/wiki_freshness.py` (new, ~220 lines).
- CLI wired via `memorymaster/cli.py` + `memorymaster/cli_handlers_curation.py`;
  subcommand added to `_NO_SERVICE_COMMANDS` since the metric is pure filesystem.
- `memorymaster/vault_linter.py` gained a `_detect_stale_articles` helper and a
  new `stale_articles` field on the lint report. The `lint-vault` handler
  prints a new "Stale articles" block after the existing "Stale claims" block.
- Tests: `tests/test_wiki_freshness.py` (12 cases, all green).
- No mutations to wiki article content; this module is strictly read-only
  over the vault.

## Interpretation for the first iteration

Because the entire vault was just absorbed, this baseline is effectively a
*calibration run* rather than a hit list. The useful signal will appear
starting around the 30–60 day mark for scopes that don't receive further
absorb activity — that's when `STALE_ARTICLE` lint warnings will start to
land and operators can react via `wiki-cleanup` or human review.

## Next steps (deferred)

Per spec, the composite B/C/D signals (claim turnover, contradiction
pressure, recall traffic) are explicitly out of scope for 11.8. Layering
them in later should keep the Option A score as the `absorb_recency`
component of the final composite so this baseline remains comparable.
