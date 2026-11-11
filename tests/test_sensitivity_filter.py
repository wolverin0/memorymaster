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


# --- audit S-1 / S-5 / S-7 / S-8 ----------------------------------------------------------------------------
# Fixtures are synthetic: made-up values, no real credential, user name or home path.
import sqlite3  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from memorymaster.core.models import CitationInput  # noqa: E402
from memorymaster.core.security import sanitize_claim_input  # noqa: E402

PW = "Xy7#kPq9"


def _cuit(prefix: str, body: str) -> str:
    """Build a CUIT/CUIL with a valid mod-11 check digit (callers pick bodies whose check is not 10)."""
    digits = prefix + body
    total = sum(int(d) * w for d, w in zip(digits, (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)))
    check = 11 - total % 11
    return digits + str(0 if check == 11 else check)


@pytest.mark.parametrize("text", [
    f"la contraseña del servidor es {PW} no la compartas",
    f"la contrasena del servidor es {PW}",
    f"clave: {PW}",
    f"la clave de acceso es {PW}",
    f"usuario admin clave {PW}",
    f"la clave de wifi es `{PW}` ok",
    f"password es {PW}",
    f"las contraseñas son {PW}",
    "usuario admin clave Futura2024!",
    "la clave de acceso es Admin1234",
    "la contraseña es futura2024!",
    "password es Futura2024",
    "la contraseña del wifi es `casa-norte-88`",
])
def test_spanish_prose_password_is_redacted(text: str) -> None:
    redacted, findings = redact_text(text)
    assert findings, text
    assert not any(t in redacted for t in (PW, "Futura2024", "Admin1234", "casa-norte-88")), redacted


NEGATIVE_PROSE = [
    "we pass --timeout=600 to pytest",
    "pass encoding=utf8 to open()",
    "all 25 tests pass 100%",
    "pass n_jobs=4",
    "pass arg1 then arg2",
    "hash the password with sha256 first",
    "the password policy requires min_length=12",
    "password hashing=bcrypt2",
    "store the password as argon2id",
    "reset the password via /api/v2/reset",
    "la clave es utf8mb4 en la conexión",
    "la clave es usar http2",
    "la clave es python3",
    "la clave del cache es sha256",
    "la clave primaria es order_id2",
    "clave foránea user_id2",
    "la clave pública es ed25519",
    "la clave de API se rota cada 90 días",
    "contraseña incorrecta 3 veces",
    "la clave es admin1234",
]


@pytest.mark.parametrize("text", NEGATIVE_PROSE)
def test_ordinary_coding_prose_survives_claim_sanitizing(text: str) -> None:
    result = sanitize_claim_input(text=text, object_value=None, citations=[CitationInput("audit://x", "x")])
    assert result.text == text and result.findings == [], (text, result.findings)


@pytest.mark.parametrize("text", [
    "la clave es reiniciar el servicio",
    "la clave del éxito es la constancia",
    "contraseña incorrecta, probá de nuevo",
    "hay que pedir la contraseña al cliente",
    "cambiar la clave primaria de la tabla",
    "la clave foránea apunta a clientes",
    "la clave pública se publica en el servidor",
    "la clave privada nunca se comparte",
    "palabra clave: facturación",
    "la clave es Python",
    "la clave es user_id",
    "clave única compuesta por cuit y número",
    "la clave de api es obligatoria",
    "la clave es la versión v2.0.1 del esquema",
])
def test_spanish_password_words_in_ordinary_prose_stay_readable(text: str) -> None:
    redacted, findings = redact_text(text)
    assert findings == [], (text, findings)
    assert redacted == text


@pytest.mark.parametrize("scheme", ["https", "http", "ftp", "smb", "redis", "amqp", "mqtt", "rtsp", "ssh", "sftp"])
def test_any_scheme_userinfo_password_is_redacted_user_and_host_stay(scheme: str) -> None:
    redacted, findings = redact_text(f"fetch {scheme}://svcuser:{PW}@host.example.test/path now")
    assert findings
    assert PW not in redacted
    if scheme not in {"redis", "amqp"}:  # those keep the legacy whole-URL db_url_password redaction
        assert f"{scheme}://svcuser:" in redacted and "@host.example.test/path" in redacted


@pytest.mark.parametrize("text", [
    "clone https://github.com/org/repo.git",
    "open http://localhost:8080/health",
    "see https://example.test:8443/a@b",
    "git remote ssh://git@github.com:22/org/repo",
    "email me@example.test about http://example.test:80 please",
    "use https://user:${PASSWORD}@host.example.test/x",
    "use https://user:<password>@host.example.test/x",
])
def test_urls_without_embedded_password_stay_readable(text: str) -> None:
    redacted, findings = redact_text(text)
    assert findings == [], (text, findings)
    assert redacted == text


AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
AWS_ID = "AKIA" + "ZQ7LMN4PRS6TUV2W"


@pytest.mark.parametrize("text", [
    f"access {AWS_ID} and secret {AWS_SECRET} for s3",
    f"{AWS_SECRET} is the secret, id {AWS_ID}",
    f"the aws secret access key is {AWS_SECRET}",
    f"AWS_SECRET_ACCESS_KEY={AWS_SECRET}",
])
def test_aws_secret_access_key_with_aws_context_is_redacted(text: str) -> None:
    redacted, findings = redact_text(text)
    assert AWS_SECRET not in redacted
    assert findings  # the AWS_SECRET_ACCESS_KEY= form is also caught by compound_credential


@pytest.mark.parametrize("text", [
    f"the s3 backup uses {AWS_SECRET} as credentials",           # no aws context at all
    "commit 3fa9c1d7e4b2a8f0c6d5e1b9a7f3c2d4e6b8a0c1 on aws",     # git sha (40 hex) next to the word aws
    "aws " + "a" * 40,                                            # single case, no digit
])
def test_bare_or_hex_40_char_tokens_are_not_an_aws_secret(text: str) -> None:
    _, findings = redact_text(text)
    assert "aws_secret_key" not in findings


@pytest.mark.parametrize("text, digits", [
    ("OTP 493021", "493021"),
    ("el OTP code is 493021 send it", "493021"),
    ("código de verificación: 493021", "493021"),
    ("verification code is 493021", "493021"),
    ("PIN: 4821", "4821"),
    ("el pin es 4821", "4821"),
    ("cajero PIN 4821 del cliente", "4821"),
])
def test_otp_and_pin_with_keyword_are_redacted(text: str, digits: str) -> None:
    redacted, findings = redact_text(text)
    assert "otp_pin" in findings
    assert digits not in redacted


@pytest.mark.parametrize("text", [
    "pedido 493021 entregado",
    "total 4821 pesos",
    "the pin header has 40 pins",
    "pin 17 of the GPIO header",
    "PIN is required for the signup form",
    "puerto 4821 abierto",
    "OTP support lands in 2026",
    "el código de verificación se envía por mail",
])
def test_bare_numbers_and_keywords_without_digits_stay_readable(text: str) -> None:
    redacted, findings = redact_text(text)
    assert findings == [], (text, findings)
    assert redacted == text


def test_zero_width_characters_do_not_hide_a_secret() -> None:
    text = "password=" + PW[:3] + "\u200b" + PW[3:]
    redacted, findings = sanitize_persisted_text(text)
    assert findings
    assert PW[3:] not in redacted


def test_zero_width_joiner_in_emoji_text_is_untouched() -> None:
    text = "family \U0001F468\u200d\U0001F469\u200d\U0001F467 ok"
    assert sanitize_persisted_text(text) == (text, [])


@pytest.mark.parametrize("cuit", [
    _cuit("20", "27123456"),
    _cuit("27", "30123456"),
    _cuit("30", "71234567"),
    _cuit("33", "50123456"),
])
def test_valid_cuit_cuil_is_redacted_dashed_and_plain(cuit: str) -> None:
    for shape in (f"{cuit[:2]}-{cuit[2:10]}-{cuit[10]}", cuit):
        redacted, findings = redact_text(f"CUIT {shape} del cliente")
        assert "argentine_cuit_cuil" in findings, shape
        assert shape not in redacted
    _, findings = redact_text(f"la persona {cuit} cobra")
    assert "argentine_cuit_cuil" in findings


def test_cuit_shaped_numbers_that_fail_the_check_digit_or_prefix_stay_readable() -> None:
    good = _cuit("20", "27123456")
    bad = good[:10] + str((int(good[10]) + 1) % 10)
    for text in (
        f"CUIT {bad[:2]}-{bad[2:10]}-{bad[10]}",     # wrong check digit
        f"numero {bad}",                              # wrong check digit, plain
        f"CUIT {_cuit('21', '27123456')}",            # valid check digit, prefix not in the list
        "llamar al 11 4567 8901 234",                 # phone
        "epoch 1700000000000",                        # timestamp, 13 digits
        f"id {good}0",                                # 12 digits: not a CUIT
    ):
        redacted, findings = redact_text(text)
        assert "argentine_cuit_cuil" not in findings, text
        assert redacted == text


@pytest.mark.parametrize("path, user", [
    ("log at C:\\Users\\pauolx01\\AppData\\x.log", "pauolx01"),
    ("file C:/Users/pauolx02/Desktop/x.txt", "pauolx02"),
    ("file c:/users/pauolx03/Desktop/x.txt", "pauolx03"),
    ("see /c/Users/pauolx04/projects/x", "pauolx04"),
    ("see /home/pauolx05/projects/x", "pauolx05"),
    ("see /Users/Pauolx06/projects/x", "Pauolx06"),
])
def test_personal_home_path_is_redacted_in_every_slash_style(path: str, user: str) -> None:
    redacted, findings = redact_text(path)
    assert findings
    assert user not in redacted


@pytest.mark.parametrize("text", [
    "C:/Windows/System32 and /usr/local/bin",
    "route /users/:id and /home/ page",
])
def test_non_user_paths_stay_readable(text: str) -> None:
    assert redact_text(text) == (text, [])


def test_remember_does_not_persist_a_forward_slash_home_path(tmp_path: Path) -> None:
    from memorymaster.core.service import MemoryService
    from memorymaster.public.v1 import remember

    ws = tmp_path / "ws"
    ws.mkdir()
    db = tmp_path / "s8.db"
    MemoryService(str(db), workspace_root=ws).init_db()
    user = "pauolx02"
    remember(text="note: file C:/Users/" + user + "/Desktop/x.txt", scope="project:audit",
             source_agent="auditor", db=db, workspace=ws)
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        hits = []
        for (table,) in con.execute("select name from sqlite_master where type='table'").fetchall():
            for col in [r[1] for r in con.execute(f'pragma table_info("{table}")')]:
                if con.execute(f'select 1 from "{table}" where cast("{col}" as text) like ? limit 1',
                               (f"%{user}%",)).fetchone():
                    hits.append(f"{table}.{col}")
    finally:
        con.close()
    assert hits == []


def test_ssh_key_fingerprint_is_public_and_stays_readable() -> None:
    fp = "SHA256:nThbg6kXUpJWGl7E1IGOCspRomTxdCARLviKw6E5SY8"
    text = f"fingerprint {fp} for host"
    assert redact_text(text) == (text, [])
    assert sanitize_claim_input(text=text, object_value=None,
                                citations=[CitationInput("audit://x", "x")]).text == text


def test_private_ip_policy_differs_by_writer_on_purpose() -> None:
    """redact_text (raw sources, export prefilter) keeps private IPv4; the claim writer redacts it."""
    text = "the db server is at 10.199.30.40 now"
    assert redact_text(text) == (text, [])
    claim = sanitize_claim_input(text=text, object_value=None, citations=[CitationInput("audit://x", "x")])
    assert "10.199.30.40" not in claim.text
    assert "private_ipv4" in claim.findings


def test_full_filter_on_200kb_of_benign_text_is_fast() -> None:
    line = ("la clave primaria de la tabla clientes cambia; see https://example.test:8443/a/b?c=d "
            "commit 3fa9c1d7e4b2a8f0c6d5e1b9a7f3c2d4e6b8a0c1 pin 17 total 98.765.432 pesos\n")
    blob = line * (200_000 // len(line))
    start = time.perf_counter()
    redacted, findings = sanitize_persisted_text(blob)
    elapsed = time.perf_counter() - start
    assert findings == [] and redacted == blob
    assert elapsed < 1.0, elapsed
    # the new patterns alone on adversarial runs (compound_credential, older, is quadratic on long alnum runs)
    from memorymaster.core import security

    new = {"url_basic_auth_password", "password_prose_es", "otp_pin", "aws_secret_key", "argentine_cuit_cuil",
           "home_path_windows", "home_path_unix"}
    pathological = ("a://:" * 40_000) + ("clave " * 20_000) + ("pin " * 20_000) + ("x" * 100_000) + ("1" * 100_000)
    for name, pattern in security._SECRET_PATTERNS:
        if name in new:
            start = time.perf_counter()
            list(pattern.finditer(pathological))
            assert time.perf_counter() - start < 1.0, name


@pytest.mark.parametrize("text", ["el DNI del cliente es 12345678", "DNI es 27.123.456", "DNI is 27123456"])
def test_argentine_dni_with_a_short_connector_is_redacted(text: str) -> None:
    redacted, findings = redact_text(text)
    assert "argentine_dni" in findings
    assert not any(ch.isdigit() for ch in redacted)


def test_compound_credential_is_linear_on_adversarial_input() -> None:
    from memorymaster.core import security

    pattern = dict(security._SECRET_PATTERNS)["compound_credential"]
    for blob in ("a://:" * 60_000, "x" * 300_000, "token" * 60_000, "MY_" * 100_000, "password " * 30_000):
        start = time.perf_counter()
        redact_text(blob)
        assert time.perf_counter() - start < 1.0, blob[:12]
    # same matches as before on the shapes that matter
    for text, value in (
        ("export DB_PASSWORD=Hunter2xyz", "Hunter2xyz"),
        ("PGPASSWORD=Hunter2xyz psql", "Hunter2xyz"),
        ("MY_API_KEY: 'Hunter2xyz'", "Hunter2xyz"),
        ("cfg client_secret = Hunter2xyz", "Hunter2xyz"),
    ):
        match = pattern.search(text)
        assert match and match.group(1) == value, text
