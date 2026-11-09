"""Canonical sensitivity filter: patterns added for T-2274 (Stop-hook privacy).

Rule (.claude/rules/sensitivity-filter.md): every filter change ships a case that proves the new pattern is caught.
The ``redact_text`` result is the contract; the negative cases pin the false-positive guard so the patterns do not
swallow ordinary prose (hashes, long identifiers, money amounts, plain version numbers).
"""
from __future__ import annotations

import pytest

from memorymaster.core.security import redact_text, sanitize_persisted_text, scan_text_for_findings

BASE64_SECRET = "c2VjcmV0LXZhbHVlLWZvci10ZXN0aW5nLTEyMzQ1Njc4OTA="


# --- base64_secret ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("blob", [
    BASE64_SECRET,                                              # padded, 48 chars
    "QWxhZGRpbjpvcGVuIHNlc2FtZQ12345678abcdXYZ==",              # padded with ==
    "dGhpc0lzQVNlY3JldFZhbHVlVGhhdE5vYm9keVNob3VsZFNlZTEyMw",   # unpadded, 54 chars, mixed case + digits
])
def test_base64_blob_is_redacted(blob: str) -> None:
    redacted, findings = redact_text(f"deploy notes: {blob} (kept private)")
    assert "base64_secret" in findings
    assert blob not in redacted


@pytest.mark.parametrize("text", [
    "commit 9fceb02d0ae598e95dc970b74767f19372d61af8 fixed it",              # 40-char lowercase hex sha
    "sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # 64 hex
    "see memorymaster/knowledge/transcript_miner/rule_miner.py for the code",  # path, slashes and underscores
    "TestSomethingVeryLongNameWithManyWordsAndNoDigits is a class",           # no digit
    "the key sha512-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKlMnOpQr is an integrity tag",  # '-' glued
    "short Zm9vYmFy token is a tiny base64 word",                              # < 24 chars
])
def test_base64_pattern_leaves_ordinary_text_alone(text: str) -> None:
    redacted, findings = redact_text(text)
    assert "base64_secret" not in findings
    assert redacted == text


def test_base64_blob_is_neutralized_before_it_reaches_a_persisted_field() -> None:
    sanitized, findings = sanitize_persisted_text(f"token blob {BASE64_SECRET} end")
    assert BASE64_SECRET not in sanitized
    assert "base64_secret" in findings
    assert "base64_secret" in scan_text_for_findings(BASE64_SECRET)


# --- argentine_dni ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, digits", [
    ("DNI 12.345.678", "12.345.678"),
    ("el dni: 12345678 del titular", "12345678"),
    ("D.N.I. N° 9.876.543", "9.876.543"),
    ("Documento Nacional de Identidad 30123456", "30123456"),
    ("documento nro 27.654.321", "27.654.321"),
])
def test_argentine_dni_with_context_is_redacted(text: str, digits: str) -> None:
    redacted, findings = redact_text(text)
    assert "argentine_dni" in findings
    assert digits not in redacted


@pytest.mark.parametrize("text", [
    "the invoice total was $12.345.678 pesos",       # ARS money format: same digits, no document context
    "ticket 12345678 was closed",                    # 8-digit id, no context
    "version 12.345.678 of the schema",              # dotted number, no context
    "DNI is required for the signup form",           # keyword without a number
    "DNI 123 is not a document number",              # too short
    "documento 12 de marzo",                         # 'documento' + a date, not a 7-8 digit run
])
def test_argentine_dni_needs_the_document_context(text: str) -> None:
    redacted, findings = redact_text(text)
    assert "argentine_dni" not in findings
    assert redacted == text
