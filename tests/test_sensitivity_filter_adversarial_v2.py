"""V2 adversarial corpus for the sensitivity filter (2026-04-23 refresh).

Reads ``tests/fixtures/sensitivity_adversarial_v2.jsonl`` (100 synthetic
entries: 50 block + 50 pass). Every secret-shaped token is FAKE-prefixed or
obviously synthetic (4242…, AAAA…, FAKEv2…). No real credentials.

Unlike the v1 corpus, these entries were authored AFTER the filter shipped,
so they test for overfit rather than ratifying existing behaviour:

Positive categories (must block):
  * api_key_env_export / _toml_config / _url_param / _json_payload /
    _stacktrace / _shell_history — API keys hidden in unusual framings.
  * oauth_db_row / jwt_console_log — OAuth and JWT tokens in DB rows and
    browser console output.
  * password_dsn / cert_pem_body — passwords embedded in DSN strings,
    PEM private-key bodies.
  * private_ip_port_prose — private IPv4 paired with a port in prose
    (internal topology leak).
  * home_path_windows / home_path_unix — paths that reveal the user's
    account name (C:\\Users\\<name>, /home/<name>).
  * card_number_prose — Visa/Mastercard/Amex PAN shapes in prose or
    form-data dumps (all BIN digits synthetic).

Negative categories (must pass):
  * placeholder_tutorial — YOUR_API_KEY_HERE, <token>, REPLACE_ME variants.
  * prose_secret_word / product_copy — prose that mentions 'password',
    'token', 'Bearer', 'OAuth', 'JWT' without a real secret value.
  * hex_hash_not_secret / uuid_identifier / base64_public_data — shapes that
    LOOK secret-ish but are public identifiers, git SHAs, file digests, or
    base64 of known plaintext.
  * url_without_secret — regular URLs that happen to mention oauth/auth paths.
  * dollar_variable_reference — ${VAR}, {{ .Values.x }}, $VAR interpolations.

Threshold: aggregate F1 >= 0.95. If this ever drops below, DO NOT relax the
threshold — investigate and either tighten the filter or document the honest
failure in the next refresh artifact.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from memorymaster.security import redact_text

_FIXTURE = Path(__file__).parent / "fixtures" / "sensitivity_adversarial_v2.jsonl"


def _load_corpus() -> list[dict]:
    rows: list[dict] = []
    with _FIXTURE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


CORPUS = _load_corpus()
assert len(CORPUS) == 100, f"v2 corpus must have 100 entries, got {len(CORPUS)}"
_BLOCKS = [r for r in CORPUS if r["label"] == "block"]
_PASSES = [r for r in CORPUS if r["label"] == "pass"]
assert len(_BLOCKS) == 50 and len(_PASSES) == 50, (
    f"expected 50 block + 50 pass, got {len(_BLOCKS)} + {len(_PASSES)}"
)


def _predict_block(text: str) -> bool:
    _, findings = redact_text(text)
    return bool(findings)


def _short(t: str, n: int = 40) -> str:
    c = t.replace("\n", "\\n").replace("\r", "\\r")
    return c if len(c) <= n else c[:n] + "..."


def _compute_f1(rows: list[dict]) -> tuple[int, int, int, int, float, float, float]:
    tp = fp = fn = tn = 0
    for r in rows:
        pred = _predict_block(r["text"])
        actual = r["label"] == "block"
        if pred and actual:
            tp += 1
        elif pred and not actual:
            fp += 1
        elif not pred and actual:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return tp, fp, fn, tn, precision, recall, f1


def test_v2_overall_f1_meets_threshold() -> None:
    tp, fp, fn, tn, precision, recall, f1 = _compute_f1(CORPUS)
    assert f1 >= 0.95, (
        f"v2 F1={f1:.3f} P={precision:.3f} R={recall:.3f} "
        f"tp={tp} fp={fp} fn={fn} tn={tn}"
    )


def test_v2_per_category_report(capsys: pytest.CaptureFixture[str]) -> None:
    """Print per-category F1 for diagnostic visibility (never asserts)."""
    per_cat: dict[str, dict[str, int]] = defaultdict(
        lambda: {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    )
    for r in CORPUS:
        pred = _predict_block(r["text"])
        actual = r["label"] == "block"
        cat = r["category"]
        if pred and actual:
            per_cat[cat]["tp"] += 1
        elif pred and not actual:
            per_cat[cat]["fp"] += 1
        elif not pred and actual:
            per_cat[cat]["fn"] += 1
        else:
            per_cat[cat]["tn"] += 1
    print("\nper-category breakdown:")
    for cat in sorted(per_cat):
        c = per_cat[cat]
        n = sum(c.values())
        print(
            f"  {cat:28s} n={n:2d} tp={c['tp']} fp={c['fp']} "
            f"fn={c['fn']} tn={c['tn']}"
        )


@pytest.mark.parametrize(
    "row",
    _BLOCKS,
    ids=[f"v2-block[{r['category']}]:{_short(r['text'])}" for r in _BLOCKS],
)
def test_v2_positive_is_blocked(row: dict) -> None:
    assert _predict_block(row["text"]), (
        f"expected BLOCK; category={row['category']}; text={row['text']!r}"
    )


@pytest.mark.parametrize(
    "row",
    _PASSES,
    ids=[f"v2-pass[{r['category']}]:{_short(r['text'])}" for r in _PASSES],
)
def test_v2_negative_passes_through(row: dict) -> None:
    assert not _predict_block(row["text"]), (
        f"expected PASS; category={row['category']}; text={row['text']!r}"
    )
