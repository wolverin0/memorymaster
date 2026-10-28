"""Ingest sensitivity filter: regression cases from AUDIT-2026-10-28.

`.claude/rules/sensitivity-filter.md` names this file as the home for filter
cases. Passing tests pin behaviour that already holds; the skipped ones are
red bars for bugs documented in AUDIT-2026-10-28.md and must be un-skipped
together with the fix in `memorymaster/core/security.py`.
"""

from __future__ import annotations

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.security import redact_text, scan_text_for_findings
from memorymaster.core.service import MemoryService

# Built at runtime so the literal never matches a repository secret scanner.
_PW = "Xk9#" + "mQ2vLp"


def _ingest(tmp_path, text: str):
    svc = MemoryService(tmp_path / "filter.db", workspace_root=tmp_path)
    svc.init_db()
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="audit-test")],
        scope="project:audit",
        source_agent="audit-test",
    )


# --- Behaviour that holds today -------------------------------------------


def test_plain_connection_string_password_is_redacted_on_ingest(tmp_path):
    claim = _ingest(tmp_path, f"prod DB is postgres://admin:{_PW}@db.example.internal:5432/app")
    assert _PW not in claim.text
    assert "[REDACTED:db_url_password]" in claim.text


def test_placeholder_only_connection_string_is_not_flagged():
    # Tutorial placeholders must stay suppressed (docs are not credentials).
    _, findings = redact_text("postgres://user:password@localhost/mydb")
    assert "db_url_password" not in findings


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abcd1234efgh5678ijkl",
        "use the key AKIA" + "IOSFODNN7EXAMPLE",
        "token: gh" + "p_" + "a" * 40,
        "sk-" + "proj-Ab12Cd34Ef56Gh78Ij90Kl12",
    ],
)
def test_known_token_families_are_detected(text):
    assert scan_text_for_findings(text)


# --- AUDIT-2026-10-28 R1: placeholder marker anywhere in the match ----------

_R1 = (
    "AUDIT-2026-10-28 R1: _match_is_suppressed tests placeholder markers against "
    "match.group(0); db_url_password ends in greedy [^\\s]+, so a trailing $VAR, "
    "<word> or {{x}} in the URL suppresses the whole finding and the password "
    "is stored verbatim as a public claim."
)


@pytest.mark.skip(reason=_R1)
@pytest.mark.parametrize(
    "suffix",
    [
        "?user=$USER",  # shell variable in the query string
        "/<dbname>",  # MongoDB Atlas / docs copy-paste shape
        "?app={{app_name}}",  # template interpolation
    ],
)
def test_connection_string_password_redacted_despite_placeholder_suffix(suffix):
    text = f"prod DB is postgres://admin:{_PW}@db.example.internal:5432/app{suffix}"
    redacted, findings = redact_text(text)
    assert "db_url_password" in findings
    assert _PW not in redacted


@pytest.mark.skip(reason=_R1)
def test_ingest_does_not_persist_password_when_url_has_placeholder(tmp_path):
    claim = _ingest(tmp_path, f"prod DB is postgres://admin:{_PW}@db.example.internal:5432/app?user=$USER")
    assert _PW not in claim.text


# --- AUDIT-2026-10-28 R2: token families without a pattern ------------------

_R2 = (
    "AUDIT-2026-10-28 R2: no _SECRET_PATTERNS entry for GitLab (glpat-), "
    "Hugging Face (hf_) or npm (npm_) access tokens; they are stored verbatim."
)


@pytest.mark.skip(reason=_R2)
@pytest.mark.parametrize(
    "token",
    [
        "glp" + "at-AbCdEfGh12345678IjKl",
        "h" + "f_AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        "np" + "m_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    ],
)
def test_uncovered_token_families_are_detected(token):
    assert scan_text_for_findings(f"CI uses {token} for publishing")


# --- AUDIT-2026-10-28 R3: PEM private key body survives redaction ------------

_R3 = (
    "AUDIT-2026-10-28 R3: private_key only matches the '-----BEGIN ... PRIVATE "
    "KEY-----' header line; the base64 body and END line are stored, and PGP "
    "'PRIVATE KEY BLOCK' headers are not matched at all."
)
_PEM_BODY = "\n".join(["MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn" + "/ygWyF8PbnGy0AHB7MaYkzK6Wl3XUvQ8m"] * 3)


def test_pem_header_is_flagged():
    _, findings = redact_text("-----BEGIN RSA PRIVATE KEY-----\n" + _PEM_BODY)
    assert "private_key" in findings


@pytest.mark.skip(reason=_R3)
def test_pem_private_key_body_is_not_persisted(tmp_path):
    pem = f"deploy key:\n-----BEGIN RSA PRIVATE KEY-----\n{_PEM_BODY}\n-----END RSA PRIVATE KEY-----"
    claim = _ingest(tmp_path, pem)
    assert "MIIEowIBAAKCAQEA" not in claim.text


@pytest.mark.skip(reason=_R3)
def test_pgp_private_key_block_is_flagged():
    assert scan_text_for_findings("-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF0abc\n-----END PGP PRIVATE KEY BLOCK-----")
