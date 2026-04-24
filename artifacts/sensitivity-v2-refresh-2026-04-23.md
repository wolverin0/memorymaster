# Sensitivity filter — v2 adversarial refresh (2026-04-23)

## Task

Roadmap item 4.1 Wave C. Build a NEW 100-entry adversarial corpus (50 block
+ 50 pass) distinct from `sensitivity_adversarial.jsonl` (v1) to probe for
overfit in `memorymaster/security.py::redact_text`. Re-measure F1 and, if the
filter regresses, tighten it rather than relax the threshold.

Branch: `omni/feat-sensitivity-v2-2026-04-23` (base `3a34b2d`, worktree
`.claude/worktrees/agent-a33da209`).

## Headline numbers

| Corpus | TP | FP | FN | TN | Precision | Recall | **F1** |
|--------|----|----|----|----|-----------|--------|--------|
| v1 (pre-change) | 99 | 0 | 1 | 100 | 1.000 | 0.990 | **0.995** |
| v1 (post-change) | 99 | 0 | 1 | 100 | 1.000 | 0.990 | **0.995** |
| v2 (baseline, pre-change) | 34 | 5 | 16 | 45 | 0.872 | 0.680 | **0.764** |
| v2 (post-change) | 50 | 0 | 0 | 50 | 1.000 | 1.000 | **1.000** |

- v1 F1 is preserved at 0.995 (the single remaining FN is the pre-existing
  `_KNOWN_HARD_FN` xfail case `bcrypt-seed=PasswordLooksR3al`, documented in
  `tests/test_sensitivity_filter_adversarial.py`).
- v2 baseline F1 of 0.764 confirms that v1 was moderately overfit: 16 of 50
  positive traps slipped through the filter unchanged, plus 5 negatives were
  falsely blocked.
- After targeted filter additions (NOT threshold relaxations), v2 reaches
  1.000 without degrading v1.

## v2 per-category F1 (post-change)

All 22 categories score F1 = 1.00. Diagnostic test
`test_v2_per_category_report` prints the breakdown on every run.

### Positive categories (block, n=50)

| Category | n | TP | FN | F1 |
|---|---|---|---|---|
| api_key_env_export | 4 | 4 | 0 | 1.00 |
| api_key_toml_config | 4 | 4 | 0 | 1.00 |
| api_key_url_param | 5 | 5 | 0 | 1.00 |
| api_key_json_payload | 4 | 4 | 0 | 1.00 |
| api_key_stacktrace | 2 | 2 | 0 | 1.00 |
| api_key_shell_history | 1 | 1 | 0 | 1.00 |
| oauth_db_row | 5 | 5 | 0 | 1.00 |
| jwt_console_log | 3 | 3 | 0 | 1.00 |
| password_dsn | 5 | 5 | 0 | 1.00 |
| cert_pem_body | 2 | 2 | 0 | 1.00 |
| private_ip_port_prose | 5 | 5 | 0 | 1.00 |
| home_path_windows | 3 | 3 | 0 | 1.00 |
| home_path_unix | 3 | 3 | 0 | 1.00 |
| card_number_prose | 4 | 4 | 0 | 1.00 |

### Negative categories (pass, n=50)

| Category | n | TN | FP | F1 |
|---|---|---|---|---|
| placeholder_tutorial | 10 | 10 | 0 | 1.00 |
| prose_secret_word | 8 | 8 | 0 | 1.00 |
| product_copy | 6 | 6 | 0 | 1.00 |
| hex_hash_not_secret | 7 | 7 | 0 | 1.00 |
| uuid_identifier | 6 | 6 | 0 | 1.00 |
| base64_public_data | 5 | 5 | 0 | 1.00 |
| url_without_secret | 5 | 5 | 0 | 1.00 |
| dollar_variable_reference | 3 | 3 | 0 | 1.00 |

## Baseline v2 failures (before filter changes)

Five most-interesting false negatives (v1-overfit cases, fixture text
abbreviated to obscure synthetic secret values):

1. `oauth_db_row` — `INSERT INTO oauth_tokens VALUES (.., '[ghp_... 44 chars
   ...]', ..)`. The `github_token` regex required exactly `{36}` chars followed
   by `\b`; any synthetic token longer than 36 body chars failed the boundary
   check.
2. `private_ip_port_prose` — `the Postgres primary is at 10.0.5.3:5432 on the
   admin VLAN`. Bare private IPs are intentionally ignored at ingest time (see
   comment in `security.py`), but IP+port combinations leak topology in
   addition to network class.
3. `home_path_windows` — `C:\Users\<user>\.aws\credentials`. Path-revealed
   usernames had no pattern at all.
4. `home_path_unix` — `/home/<user>/.ssh/id_rsa`. Same gap as Windows home
   paths.
5. `card_number_prose` — `refund card 4242-4242-4242-4242 exp 12/29 cvv 123`.
   PANs had no pattern at all; v1 never tested for card data.

Five most-interesting false positives (filter over-blocking prose):

1. `prose_secret_word` — "tokens: they live in KMS-encrypted secrets". The
   `compound_credential` regex captured `they` as the value — a 4-char English
   word.
2. `placeholder_tutorial` — "set STRIPE_SECRET=YOUR_STRIPE_KEY_HERE". The
   `YOUR_[KEY\|TOKEN\|...]_HERE` placeholder marker required `YOUR_KEY_HERE`
   to be adjacent, but real tutorials insert vendor words in the middle.
3. `product_copy` — "The CLI supports Bearer authentication and will prompt".
   The `bearer_token` regex matched `Bearer authentication`; 14 chars of
   lowercase English letters satisfied `[A-Za-z0-9_\-\.]{8,}`.
4. `dollar_variable_reference` — `apiKey: ${OPENAI_API_KEY}`. The
   `token_assignment` regex captured `${OPENAI_API_KEY}` as a value.
5. `dollar_variable_reference` — `password: {{ .Values.password }}`. The
   `password_assignment` regex captured the Helm template expression as a
   value.

## Filter changes made

All changes are in `memorymaster/security.py`. Every change carries a
`v2-refresh (<category>)` comment so future readers know the source trap.

| Line | Change | Driver category | Direction |
|------|--------|-----------------|-----------|
| 26 | `{36}` → `{36,}` on the ghX_ token regex | `oauth_db_row` | +recall |
| 35 | Bearer regex now requires value to contain a digit, underscore, hyphen, or dot | `product_copy` | +precision |
| 95 | Placeholder marker regex: inserted optional `(?:[A-Z]+[_\-])?` segment so `YOUR_STRIPE_KEY_HERE` matches | `placeholder_tutorial` | +precision |
| 99-100 | Placeholder marker regex: added `${VAR}`, `{{ expr }}`, `$VAR` alternatives (case-sensitive via `(?-i:)`) | `dollar_variable_reference` | +precision |
| 110-128 | New helper `_is_low_entropy_value` + `_STRUCTURED_CRED_FINDINGS` set; `_redact` now suppresses structured-credential matches whose captured value is <6 chars OR all-lowercase-letters | `prose_secret_word`, `product_copy` | +precision |
| 130-147 | Refactored `_redact` to per-match substitution so a placeholder-only occurrence in a text that also has legitimate matches no longer eats the legitimate match | (consequence of 110-128) | structural |
| 220-248 | Four new `_SECRET_PATTERNS`: `private_ip_port`, `home_path_windows`, `home_path_unix`, `card_number_pan` | `private_ip_port_prose`, `home_path_windows`, `home_path_unix`, `card_number_prose` | +recall |

Nothing was removed, no test case was dropped, no threshold was relaxed,
and `allow_sensitive_bypass` flags remain untouched.

## Subtle fix worth flagging

The first attempt at the template-literal placeholder added
`\$[A-Z][A-Z0-9_]*\b` directly to the existing `(?i)…` regex. The leading
case-insensitive flag bled into the new alt, turning `[A-Z]` into
`[A-Za-z]` — which then matched legitimate credentials like
`PGPASSWORD=Lemur9Pattern$Zzz` (the `$Zzz` tail satisfied the lowercased
`[A-Z]+`). The fix uses an inline `(?-i:…)` group to clamp the case
sensitivity back on. The bug was caught by v1 running green again after the
fix — a good reminder that an adversarial suite is only as good as how often
you re-run it.

## Is the filter over- or under-fit?

Before this refresh: **over-fit to v1's exact phrasings**. v1 framed positives
in single-line marker-value shapes (`TOKEN=value`, `the password is X`), so
the filter developed weak multi-line, multi-context, and structural-context
awareness. v2 exposed this by framing the same secret types in TOML,
stack-trace, SQL-dump, browser-console, and DSN shapes.

After this refresh: **well-calibrated on the union of v1 + v2**, but we
should assume v3 will find more gaps. Obvious next-round traps not covered
here:

- Unicode-confusable credential keys (`раssword` with Cyrillic `а`).
- Multi-part credentials where parts are >80 chars apart (longer prose leaks).
- Hex-encoded tokens without a trailing boundary (`0x` + 64 hex in the middle
  of a word).
- Kubernetes `Secret` YAML where values are base64-encoded.
- Private RSA keys in single-line form (no newlines).

Filing these here as the seed for a hypothetical `sensitivity_adversarial_v3`.

## Verification

```
python -m pytest tests/test_sensitivity_filter_adversarial.py -v
  → 200 passed, 1 xfailed in 0.21s   (v1 preserved, F1 0.995)

python -m pytest tests/test_sensitivity_filter_adversarial_v2.py -v
  → 102 passed in 0.16s               (v2 F1 1.000)

python -m pytest tests/ -k "security or sensitivity or redact" -q
  → 372 passed, 2 skipped, 1 xfailed in 11.72s

python -m pytest tests/ -q
  → 1448 passed, 39 skipped, 1 xfailed in 169.82s
```

No broader regressions.

## Artifacts produced

- `tests/fixtures/sensitivity_adversarial_v2.jsonl` — new corpus (100 lines,
  50 block + 50 pass, every secret synthetic or FAKE-prefixed).
- `tests/test_sensitivity_filter_adversarial_v2.py` — parametrized suite,
  per-category diagnostic, aggregate F1 assertion at ≥ 0.95.
- `memorymaster/security.py` — filter additions (all with `v2-refresh (cat)`
  comments).
- `artifacts/sensitivity-v2-refresh-2026-04-23.md` — this file.
