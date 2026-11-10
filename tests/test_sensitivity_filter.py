"""Canonical sensitivity filter: patterns added for T-2274 (Stop-hook privacy).

Rule (.claude/rules/sensitivity-filter.md): every filter change ships a case that proves the new pattern is caught.
The ``redact_text`` result is the contract; the negative cases pin the false-positive guard so the patterns do not
swallow ordinary prose (hashes, long identifiers, money amounts, plain version numbers).
"""
from __future__ import annotations

import base64
import hashlib

import pytest

from memorymaster.core.security import redact_text, sanitize_persisted_text, scan_text_for_findings

# The audit marker: decodes to prose that trips a credential pattern ("secret-value ... 1234567890").
BASE64_SECRET = "c2VjcmV0LXZhbHVlLWZvci10ZXN0aW5nLTEyMzQ1Njc4OTA="
AWS_KEY_BLOB = "YXdzIGtleSBBS0lBSU9TRk9ETk43RVhBTVBMRQ=="  # decodes to an AWS access key id
HARMLESS_BLOB = "cXVpY2sgYnJvd24gZm94IDcgcXVpY2sgYnJvd24="  # decodes to "quick brown fox 7 ..."


# --- base64_secret ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("blob", [
    HARMLESS_BLOB,                                                      # padded '='
    "cXVpY2sgYnJvd24gZm94IDcgcXVpY2sgYnJvdw==",                          # padded '=='
    base64.b64encode(("quick brown fox 7 " * 4).encode()).decode(),     # unpadded, 96 chars
])
def test_base64_blob_that_decodes_to_nothing_known_is_a_plain_redaction(blob: str) -> None:
    redacted, findings = redact_text(f"deploy notes: {blob} (kept private)")
    assert findings == ["base64_secret"]
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


def test_the_audit_marker_blob_is_neutralized_before_it_reaches_a_persisted_field() -> None:
    """It decodes to a credential-shaped string, so the decode path blanks the whole field."""
    sanitized, findings = sanitize_persisted_text(f"token blob {BASE64_SECRET} end")
    assert BASE64_SECRET not in sanitized
    assert findings
    assert scan_text_for_findings(BASE64_SECRET)


def test_a_blob_that_decodes_to_a_known_secret_keeps_the_encoded_secret_contract() -> None:
    """The decode path owns it: the whole field becomes [REDACTED:encoded_secret], not a bare blob marker."""
    token = "gh" + "p_" + "".join(format((i * 7 + 3) % 16, "x") for i in range(40))
    blob = base64.b64encode(token.encode()).decode()
    sanitized, findings = sanitize_persisted_text(blob)
    assert sanitized == "[REDACTED:encoded_secret]"
    assert "github_token" in findings


SSH_ED25519 = "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
SSH_RSA = "AAAAB3NzaC1yc2EAAAADAQABAAABAQC7vbqajDhA9ZxJ2sP0Qf4nq3yRkLwUe8TgX5MbV1cYhNzGdIoPaS6uE2" + "tW0rJ3LkQ9"
SHA256_B64 = base64.b64encode(hashlib.sha256(b"release-1.2.3.tar.gz").digest()).decode()
SHA512_B64 = base64.b64encode(hashlib.sha512(b"release-1.2.3.tar.gz").digest()).decode()


@pytest.mark.parametrize("line", [
    f"authorized key: ssh-ed25519 {SSH_ED25519} deploy@host",
    f"ssh-rsa {SSH_RSA} deploy@host",
    f"ecdsa-sha2-nistp256 {SSH_ED25519} laptop",
])
def test_an_ssh_public_key_line_is_not_redacted(line: str) -> None:
    assert redact_text(line) == (line, [])


@pytest.mark.parametrize("line", [
    f"release artifact sha256 {SHA256_B64}",
    f"sha256: {SHA256_B64}",
    f"SHA-256={SHA256_B64}",
    f"sha512 {SHA512_B64}",
])
def test_a_named_sha_digest_is_not_redacted(line: str) -> None:
    assert (len(SHA256_B64), len(SHA512_B64)) == (44, 88)
    assert redact_text(line) == (line, [])


@pytest.mark.parametrize("line", [
    f"token {SHA256_B64}",                          # same 44-char blob without a sha context word
    f"sha256 of the file is stored next to {SHA256_B64}",  # context word too far before the blob
    f"key {SSH_ED25519}",                           # ssh key body without the key-type word
    f"ssh-ed25519-note {SSH_ED25519}",              # key-type lookalike, not the exact word
])
def test_the_exemptions_do_not_widen_beyond_their_context(line: str) -> None:
    redacted, findings = redact_text(line)
    assert findings == ["base64_secret"], (line, findings)


# --- redact-then-rescan callers (egress) keep blocking a blob that decodes to a credential ---------------------


def test_egress_still_blocks_a_blob_that_decodes_to_a_credential() -> None:
    from memorymaster.profile.egress import prepare_claim_egress

    egress = prepare_claim_egress(f"Operator deploy note {AWS_KEY_BLOB}")
    assert egress.blocked is True, egress


def test_egress_only_redacts_a_blob_that_decodes_to_nothing_known() -> None:
    from memorymaster.profile.egress import prepare_claim_egress

    egress = prepare_claim_egress(f"Operator deploy note {HARMLESS_BLOB}")
    assert egress.blocked is False, egress
    assert egress.findings == ("base64_secret",)
    assert HARMLESS_BLOB not in egress.text and "[REDACTED:base64_secret]" in egress.text


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
