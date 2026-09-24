"""Egress redactor: every review-A r5 / review-B r3 leak is closed, r4 benign text survives.

Cases are copied from .planning/audits/2026-09-23-weekly-claude-review/evidence:
review-A/r5_query_egress.py (F-16), review-B/r3_redact.py and r4_falsepos.py (F-10).
All secrets below are synthetic.
"""
from __future__ import annotations

import base64
import urllib.parse

import pytest

from memorymaster.decisions.egress import (
    CREDENTIAL_FINDINGS,
    MAX_FIELD_CHARS,
    EgressText,
    prepare_egress,
    prepare_egress_value,
    shannon_entropy,
)

FAKE = "sk-proj-AbC123dEf456GhI789jKl012MnO345pQr"
HEX = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"
B64 = base64.b64encode(FAKE.encode()).decode()
URLQ = urllib.parse.quote(FAKE, safe="").replace("-", "%2D")
BS = chr(92)

# --- review-A r5_query_egress.py (F-16): query text sent to TypeSafe -------------------
R5_CASES = {
    "home_path": (r"release for C:\Users\jdoe\clients\acme-secret\build.ps1", ["jdoe"]),
    "private_ips": ("release to 10.20.30.40 and 192.168.1.10 via wg0", ["10.20.30.40", "192.168.1.10"]),
    "third_party_email": ("release notes for jane.doe@clientcorp.com", ["jane.doe@clientcorp.com", "jane.doe"]),
    "password": ("release with password=hunter2", ["hunter2"]),
}

# --- review-B r3_redact.py (F-10): candidates that passed the hook filter ---------------
R3_CASES = {
    "lower_hex_token": (f"The webhook secret is {HEX} for the ingest service", [HEX]),
    "lower_alnum_token": (
        "set the api key to abcdefghijklmnopqrstu12345xyz then restart",
        ["abcdefghijklmnopqrstu12345xyz"],
    ),
    "base64_token": ("key stored as " + B64, [B64, FAKE, "AbC123dEf456GhI789jKl012MnO345pQr"]),
    "url_encoded": ("call ?key=" + URLQ, [URLQ, FAKE, "AbC123dEf456GhI789jKl012MnO345pQr"]),
    "split_token": ("token is sk-proj-AbC123dEf456 GhI789jKl012MnO345pQr", ["AbC123dEf456", "GhI789jKl012MnO345pQr"]),
    "hyphen_split": ("the key sk-proj AbC123dEf456GhI789jKl012MnO345pQr", ["AbC123dEf456GhI789jKl012MnO345pQr"]),
    "private_ip": ("The NAS lives at 192.168.1.10 port 5000", ["192.168.1.10"]),
    "ipv6_ula": ("WireGuard peer fd00:abcd::12 handles backups", ["fd00:abcd::12", "fd00:abcd"]),
    "escaped_home": ("hook reads C:\\Users\\pauol\\.claude\\settings.json", ["pauol"]),
    "json_escaped_home": ("hook reads C:\\\\Users\\\\pauol\\\\.claude\\\\settings.json", ["pauol"]),
    "fwd_home": ("hook reads C:/Users/pauol/.claude/settings.json", ["pauol"]),
    "gitbash_home": ("hook reads /c/Users/pauol/.claude/settings.json", ["pauol"]),
    "wsl_unc": ("file at " + BS * 2 + "wsl$" + BS + "Ubuntu" + BS + "home" + BS + "pauol" + BS + "x", ["pauol"]),
    "tilde_user": ("profile under ~pauol and %USERPROFILE%", ["pauol"]),
    "password_prose": ("the vaultwarden admin password is Tr0ub4dor&3 do not change", ["Tr0ub4dor&3"]),
    "pin_prose": ("router login pin 4417-2291", ["4417-2291", "4417"]),
    "hostname": ("backup target is nas01.corp.internal via smb", ["nas01.corp.internal", "nas01"]),
}

# --- review-B r4_falsepos.py (F-10 false positives): must reach the provider unchanged ---
R4_BENIGN = [
    "Decided on 2026-09-21 to keep SQLite WAL authoritative over Postgres",
    "Release 4.8.9 fixed compact hooks; deployed 2026-09-20 10:20",
    "Root cause: requests 2.32.3 drops proxies when trust_env is False",
    "Upstream docs at https://docs.python.org/3/library/subprocess.html explain timeout kill",
    "Test test_real_transport_kills_stalled_HTTP2_child_process_v2 is flaky on Windows",
    "The steward uses gemini-3.5-flash-lite for extraction",
    "Coolify listens on port 8000 and the migration took 12,345,678 rows",
]

MORE_BENIGN = [
    "Review clock 2026-09-23T00:39-03:00 equals 2026-09-23T03:39:00Z",
    "See https://github.com/wolverin0/MemoryMaster/blob/main/docs/jev-decisions.md for details",
    "Installed revision 6909841 and fix commit 55a5871d0c3e9b2a7f14c6e8d9b0a1f2c3d4e5f6 are unmerged",
    "memorymaster.decisions.engine.decide reads MEMORYMASTER_JEV_HOOK_DEADLINE_MS",
    "The KeyRotatorForProviderFleet class uses Path.home() and threading.local()",
    "Claim mm-8aef ranks 5 of 5; api.typesafe.ai answered in 437 ms",
    "Loopback 127.0.0.1 and public resolver 8.8.8.8 are fine; uuid 550e8400-e29b-41d4-a716-446655440000",
    "The password policy rotates quarterly and the PIN has 4 digits",
    "tokenizer vocabulary SentencePieceTokenizerModel32000 loaded from src/components/dashboard/widgetsPanel",
    "path G:\\\\_OneDrive\\\\OneDrive\\\\Desktop stays readable",
]


def _assert_sanitized(result: EgressText, forbidden: list[str]) -> None:
    assert isinstance(result, EgressText)
    for value in forbidden:
        assert value not in result.text, f"{value!r} leaked in {result.text!r}"


@pytest.mark.parametrize("name", sorted(R5_CASES))
def test_review_a_r5_query_egress_is_redacted_not_leaked(name):
    text, forbidden = R5_CASES[name]
    result = prepare_egress(text)
    _assert_sanitized(result, forbidden)
    assert not result.blocked, result.reason
    assert result.text.startswith("release")
    assert sum(result.counts.values()) >= 1


def test_review_a_r5_counts_both_private_ips():
    result = prepare_egress(R5_CASES["private_ips"][0])
    assert result.counts.get("private_ip") == 2
    assert "wg0" in result.text


@pytest.mark.parametrize("name", sorted(R3_CASES))
def test_review_b_r3_candidate_leaks_are_closed(name):
    text, forbidden = R3_CASES[name]
    result = prepare_egress(text)
    _assert_sanitized(result, forbidden)
    if result.blocked:
        assert result.text == ""
    else:
        assert "[REDACTED:" in result.text


@pytest.mark.parametrize("text", R4_BENIGN + MORE_BENIGN)
def test_review_b_r4_benign_text_is_not_redacted(text):
    result = prepare_egress(text)
    assert result.text == text
    assert result.counts == {}
    assert result.blocked is False
    assert result.reason is None


def test_credential_that_survives_redaction_blocks_egress():
    encoded = "".join(f"\\x{byte:02x}" for byte in b"password=Hunter22xy")
    result = prepare_egress(f"config dump {encoded} end")
    assert result.blocked is True
    assert result.text == ""
    assert result.reason and result.reason.startswith("credential_survived:")
    assert "password_assignment" in result.reason


def test_confusable_credential_blocks_egress():
    # Cyrillic 'р' and 'а' evade the literal regexes but not the variant scan.
    result = prepare_egress("admin \u0440\u0430ssword=Hunter22xy for the box")
    assert result.blocked is True and result.text == ""


def test_credential_findings_exclude_privacy_labels():
    assert "openai_key" in CREDENTIAL_FINDINGS
    assert "password_assignment" in CREDENTIAL_FINDINGS
    assert not {"home_path_windows", "home_path_unix", "private_ip_port"} & CREDENTIAL_FINDINGS


def test_redaction_happens_before_truncation():
    text = "a " * 595 + FAKE + " tail"
    result = prepare_egress(text)
    assert len(result.text) <= MAX_FIELD_CHARS
    assert result.truncated is True
    assert "AbC123" not in result.text
    assert "sk-proj-AbC" not in result.text


def test_custom_cap_and_short_text_untouched():
    assert prepare_egress("x" * 50, max_chars=10).text == "x" * 10
    assert prepare_egress("short").truncated is False


def test_non_string_input_is_coerced_safely():
    assert prepare_egress(None).text == ""
    assert prepare_egress(12345).text == "12345"


def test_url_userinfo_and_query_secrets_are_redacted():
    result = prepare_egress("pull from https://deploy:S3cretPass@git.example.com/repo.git?x=1&token=abc123")
    assert "S3cretPass" not in result.text
    assert "deploy:" not in result.text
    assert "abc123" not in result.text
    assert "git.example.com/repo.git" in result.text
    assert "x=1" in result.text


def test_internal_hostnames_and_cgnat():
    result = prepare_egress("printer.local, db.lan, host.home.arpa and 100.101.102.103 (tailscale)")
    for value in ("printer.local", "db.lan", "host.home.arpa", "100.101.102.103"):
        assert value not in result.text


def test_posix_and_macos_home_paths():
    result = prepare_egress("logs in /home/alice/.cache and /Users/bob/Library and /mnt/c/Users/carol/x")
    for name in ("alice", "bob", "carol"):
        assert name not in result.text
    assert ".cache" in result.text


def test_windows_home_with_space_in_name_before_separator():
    result = prepare_egress(r"file C:\Users\John Smith\Documents\x.txt copied")
    assert "John" not in result.text and "Smith" not in result.text
    assert "Documents" in result.text and "copied" in result.text


def test_slack_style_url_path_secret_is_redacted_but_public_path_kept():
    # Built from parts so the committed source never holds a scannable webhook URL.
    url = "https://hooks.slack.com/services/" + "T0A1B2C3D" + "/" + "B0Z9Y8X7W" + "/" + "aB3dE5fG7hJ9kL1mN3pQ5rS7"
    result = prepare_egress(f"webhook {url}")
    assert "aB3dE5fG7hJ9kL1mN3pQ5rS7" not in result.text
    assert "hooks.slack.com/services" in result.text


def test_keyword_near_compound_identifier_still_redacts_opaque_value():
    result = prepare_egress("export API_TOKEN value Zx9Qw8Er7Ty6Ui5Op4As3Df2 now")
    assert "Zx9Qw8Er7Ty6Ui5Op4As3Df2" not in result.text


def test_unc_host_is_redacted_but_share_kept():
    result = prepare_egress("share " + BS * 2 + "nas01" + BS + "backups" + BS + "daily")
    assert "nas01" not in result.text
    assert "backups" in result.text


def test_structured_value_walks_nested_strings_and_keeps_keys():
    value = {"request": "ssh to 10.1.2.3", "items": ["mail bob@corp.example", {"n": 3, "t": "ok"}], "flag": True}
    result = prepare_egress_value(value)
    assert result.blocked is False
    assert result.value["request"] == "ssh to [REDACTED:private_ip]"
    assert result.value["items"][0] == "mail [REDACTED:email]"
    assert result.value["items"][1] == {"n": 3, "t": "ok"}
    assert result.value["flag"] is True
    assert result.counts == {"private_ip": 1, "email": 1}


def test_structured_value_blocks_when_any_field_blocks():
    encoded = "".join(f"\\x{byte:02x}" for byte in b"password=Hunter22xy")
    result = prepare_egress_value({"a": "fine", "b": [encoded]})
    assert result.blocked is True
    assert result.value is None
    assert result.reason and "password_assignment" in result.reason


def test_shannon_entropy_reference_values():
    assert shannon_entropy("") == 0.0
    assert shannon_entropy("aaaa") == 0.0
    assert shannon_entropy("abcd") == pytest.approx(2.0)


def test_huge_input_is_bounded_and_still_redacted():
    import time

    text = "ssh 10.9.8.7 " + ("lorem ipsum dolor " * 20_000)
    started = time.monotonic()
    result = prepare_egress(text)
    assert time.monotonic() - started < 2.0
    assert result.truncated and len(result.text) == MAX_FIELD_CHARS
    assert "10.9.8.7" not in result.text


def _shrinking_blob(length: int) -> str:
    """High-entropy filler that redaction shrinks to one marker (deterministic)."""
    import random
    import string

    rng = random.Random(7)
    return "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(length))


@pytest.mark.parametrize("value,label", [
    ("Zq8Xv3Lm9Pw2Rt7Ky4Hb6Nc1Jd5Fg0Sa", "val"),  # synthetic bare token, 32 chars
    ("jane.doe@clientcorp.com", "mail"),
    ("192.168.100.200", "host"),
])
@pytest.mark.parametrize("inside", [5, 12, 18, 21])
def test_value_straddling_the_scan_window_is_never_sent_as_a_fragment(value, label, inside):
    """A blob that shrinks to a marker must not pull a cut-off fragment under the cap."""
    window = MAX_FIELD_CHARS * 2 + 512
    inside = min(inside, len(value) - 1)
    blob = _shrinking_blob(window - inside - len(f" {label} "))
    result = prepare_egress(f"{blob} {label} {value} tail")
    assert result.blocked is False
    assert value[:inside] not in result.text
    assert value[:4] not in result.text
    assert result.text.endswith(f" {label} ")


@pytest.mark.parametrize("value", [
    "Zq8Xv3Lm9Pw2Rt7Ky4Hb6Nc1Jd5Fg0Sa",  # synthetic bare token, 32 chars
    "jane.doe@clientcorp.com",
    "192.168.100.200",
])
@pytest.mark.parametrize("inside", [5, 12, 18])
def test_straddling_value_without_whitespace_to_cut_at_is_dropped_with_a_marker(value, inside):
    """No whitespace in the scan window: the cut-off fragment must not be sent either."""
    window = MAX_FIELD_CHARS * 2 + 512
    prefix, middle = '{"blob":"', '","val":"'
    blob = _shrinking_blob(window - inside - len(prefix) - len(middle))
    text = f'{prefix}{blob}{middle}{value}","tail":"x"}}'
    assert not any(c.isspace() for c in text) and len(text) > window
    result = prepare_egress(text)
    assert value[:inside] not in result.text
    assert value[:4] not in result.text
    assert result.truncated
    assert result.blocked or "[REDACTED:truncated_value]" in result.text
    assert result.counts.get("truncated_value") == 1


# --- wave 3: remaining home-path spellings and phone numbers (every spelling, red first) ---

HOSTED_HOME_PATHS = {
    "smb_url": "smb://nas01/home/{u}/backups",
    "file_url_host": "file://nas01/home/{u}/notes.md",
    "http_url": "http://files.example.com/home/{u}/report.pdf",
    "https_url_port": "https://files.example.com:8443/home/{u}/report.pdf",
    "https_url_users": "https://files.example.com/Users/{u}/Desktop/x.txt",
    "unc_home": BS * 2 + "nas01" + BS + "home" + BS + "{u}" + BS + "backups",
    "unc_users": BS * 2 + "nas01" + BS + "Users" + BS + "{u}" + BS + "Desktop",
    "unc_json_escaped": BS * 4 + "nas01" + BS * 2 + "home" + BS * 2 + "{u}" + BS * 2 + "backups",
    "slash_unc_home": "//nas01/home/{u}/backups",
    "slash_unc_users": "//nas01/Users/{u}/Desktop",
    "synology_volume": "/volume1/homes/{u}/Photos",
    "synology_volume_multi_digit": "/volume12/homes/{u}/Photos",
    "qnap_share": "/share/homes/{u}/Photos",
    "bare_homes": "/homes/{u}/Photos",
    "smb_homes_share": "smb://nas01/homes/{u}/Photos",
    "nas_url_volume": "https://nas01:5001/volume1/homes/{u}/Photos",
    "percent_windows": "C%3A%5CUsers%5C{u}%5Crepo%5Cbuild.ps1",
    "percent_windows_lower": "c%3a%5cusers%5c{u}%5crepo",
    "percent_forward": "C%3A%2FUsers%2F{u}%2Frepo",
    "percent_posix": "%2Fhome%2F{u}%2Frepo",
    "percent_in_query": "https://example.com/open?path=C%3A%5CUsers%5C{u}%5Cx.txt",
    "double_percent": "C%253A%255CUsers%255C{u}%255Crepo",
    "relative_home": "./home/{u}/repo",
    "relative_parent_home": "../home/{u}/repo",
    "relative_wsl_mount": "./mnt/c/Users/{u}/repo",
    "windows_backslash": r"C:\Users\{u}\repo",
    "posix_home": "/home/{u}/repo",
    "mac_users": "/Users/{u}/Library",
    "gitbash": "/c/Users/{u}/repo",
    "wsl_mount": "/mnt/c/Users/{u}/repo",
    # E1: the ~<u> and wsl$ spellings take the whole user name too.
    "tilde_user": "~{u}/repo",
    "wsl_unc_home": BS * 2 + "wsl$" + BS + "Ubuntu" + BS + "home" + BS + "{u}" + BS + "repo",
    "wsl_localhost_home": BS * 2 + "wsl.localhost" + BS + "Ubuntu" + BS + "home" + BS + "{u}" + BS + "repo",
    "wsl_json_escaped_home": BS * 4 + "wsl$" + BS * 2 + "Ubuntu" + BS * 2 + "home" + BS * 2 + "{u}" + BS * 2 + "repo",
    "wsl_slash_home": "//wsl$/Ubuntu/home/{u}/repo",
}
TRICKY_USERS = ("alice", "dan_o'neil", "j.doe", "mary-jane", "o'brien-smith.jr")


def _user_parts(user: str) -> list[str]:
    import re

    return [part for part in re.split(r"[^A-Za-z]+", user) if len(part) >= 2]


@pytest.mark.parametrize("user", TRICKY_USERS)
@pytest.mark.parametrize("name", sorted(HOSTED_HOME_PATHS))
def test_every_home_path_spelling_hides_the_whole_user_name(name, user):
    text = f"The operator keeps the build at {HOSTED_HOME_PATHS[name].format(u=user)} for the release."
    result = prepare_egress(text)
    assert not result.blocked, result.reason
    lowered = result.text.lower()
    for part in _user_parts(user):
        assert part.lower() not in lowered, (part, result.text)
    assert "'" not in result.text, result.text  # no "'neil" left behind
    assert result.counts.get("home_path", 0) >= 1, result.counts
    assert result.text.startswith("The operator keeps the build at ") and result.text.endswith(" for the release.")


HOME_PATH_BENIGN = [
    "Docs at https://example.com/users/guide and https://example.com/c/users/x",
    "The API route GET /users/:id and /users/{id} returns the profile.",
    "See https://github.com/wolverin0/MemoryMaster/blob/main/docs/jev-decisions.md for details",
    "The share //nas01/public/installers holds the ISO; 100% done, 50%off coupon.",
    "It's the operator's call; don't change O'Neil's defaults.",
    "Encoded query ?q=hello%20world%21 and a%2Fb stay as they are.",
]


@pytest.mark.parametrize("text", HOME_PATH_BENIGN)
def test_home_path_rules_leave_ordinary_text_alone(text):
    result = prepare_egress(text)
    assert result.text == text and result.counts == {}


PHONES = {
    "e164_compact": "+5491112345678",
    "e164_ar_mobile": "+54 9 11 1234-5678",
    "e164_ar_landline": "+54 351 456 7890",
    "e164_us": "+1 415 555 2671",
    "e164_uk": "+44 20 7946 0958",
    "e164_dotted": "+54.11.4567.8901",
    "ar_caba_landline": "011 4567-8901",
    "ar_mobile_hyphens": "11-1234-5678",
    "ar_mobile_15": "11-15-1234-5678",
    "ar_mobile_space": "11 1234-5678",
    "ar_area_parens": "(0351) 456-7890",
    "ar_area_parens_caba": "(011) 4567-8901",
    "ar_area_hyphen": "0351-456-7890",
    "ar_area_space": "0351 456-7890",
    "us_parens": "(415) 555-2671",
}


@pytest.mark.parametrize("name", sorted(PHONES))
def test_phone_numbers_are_redacted(name):
    phone = PHONES[name]
    result = prepare_egress(f"Call the operator at {phone} before deploying.")
    assert not result.blocked
    digits = "".join(c for c in phone if c.isdigit())
    assert digits[-4:] not in result.text and phone not in result.text, result.text
    assert result.counts.get("phone") == 1, result.counts
    assert result.text == "Call the operator at [REDACTED:phone] before deploying."


PHONE_BENIGN = [
    "Decided on 2026-09-23 at 10:20:30 to ship 4.9.0; deployed 2026-09-20 10:20",
    "Timestamps 2026-09-23T03:39:00+00:00 and 2026-09-23T00:39-03:00 (offset -03:00, UTC+03)",
    "Dates 23-09-2026, 09/23/2026, 2026/09/23, 2026.09.23 and ISO week 2026-W39",
    "Release v1.13.0 (jev-1.13.0), Python 3.12.4, build 2026.09.23.1 and semver 10.20.30-rc.1",
    "Coolify listens on port 8443; localhost:5000, 127.0.0.1:3000 and [::1]:8080 are free",
    "claim 12345678, claim:678901, mm-8aef, commit 6909841, PR #253, issue 1234-5678-abc",
    "The request took 360-610 ms, p95 1.0-1.25 s, a 900 ms deadline and 1200-1500 ms retries",
    "uuid 550e8400-e29b-41d4-a716-446655440000 and sha 55a5871d0c3e9b2a7f14c6e8d9b0a1f2c3d4e5f6",
    "Migrated 12,345,678 rows (1234 5678 tokens) between 2024-2026 in 3 batches of 4000",
    "Scores +0.5, +1, +12 and -3.25; ratio 16:9; 4K 3840x2160; ISBN 978-3-16-148410-0",
]


@pytest.mark.parametrize("text", PHONE_BENIGN)
def test_phone_rule_leaves_dates_versions_ports_ids_and_durations_alone(text):
    result = prepare_egress(text)
    assert result.text == text, result.text
    assert "phone" not in result.counts


# --- wave 3 verifier: B2 labelled/tel: phones, B3 multi-level relative homes, B4 UNC shares ---

VERIFIER_HOME_PATHS = {
    # B3: more than one relative level
    "relative_two_levels": "../../home/{u}/repo",
    "relative_dot_parent": "./../home/{u}/repo",
    "relative_three_levels": "../../../home/{u}/x",
    "relative_inside_path": "x/../home/{u}/repo",
    "relative_wsl_two_levels": "../../mnt/c/Users/{u}/repo",
    "relative_gitbash_two_levels": "../../c/Users/{u}/repo",
    "relative_mac_two_levels": "../../Users/{u}/repo",
    "relative_synology_two_levels": "../../volume1/homes/{u}/x",
    "relative_homes_two_levels": "../../homes/{u}/x",
    "relative_in_a_command": "cd ../../home/{u} && ls",
    # B4: admin shares, a share before the home, file:// UNC URLs
    "unc_admin_share": BS * 2 + "host" + BS + "c$" + BS + "Users" + BS + "{u}" + BS + "Desktop",
    "unc_share_home": BS * 2 + "host" + BS + "share" + BS + "home" + BS + "{u}" + BS + "x",
    "unc_admin_share_json": BS * 4 + "host" + BS * 2 + "c$" + BS * 2 + "Users" + BS * 2 + "{u}",
    "slash_unc_admin_share": "//host/c$/Users/{u}/Desktop",
    "slash_unc_share_home": "//host/share/home/{u}/x",
    "file_unc_four_slashes": "file:////host/home/{u}/x",
    "file_unc_five_slashes": "file://///host/home/{u}/x",
    "file_url_admin_share": "file://host/c$/Users/{u}/x",
    "smb_share_home": "smb://nas/data/home/{u}/x",
    # cheap non-blocking extras
    "var_home": "/var/home/{u}/x",
    "export_home": "/export/home/{u}/x",
    "webdav_home": "https://host/webdav/home/{u}/x",
    "windows_relative": ".." + BS + ".." + BS + "Users" + BS + "{u}" + BS + "x",
    "windows_dot_relative": "." + BS + "Users" + BS + "{u}" + BS + "x",
    "documents_and_settings": "C:" + BS + "Documents and Settings" + BS + "{u}" + BS + "x",
}


@pytest.mark.parametrize("user", TRICKY_USERS)
@pytest.mark.parametrize("name", sorted(VERIFIER_HOME_PATHS))
def test_verifier_home_path_spellings_hide_the_whole_user_name(name, user):
    text = f"The operator keeps the build at {VERIFIER_HOME_PATHS[name].format(u=user)} for the release."
    result = prepare_egress(text)
    assert not result.blocked, result.reason
    lowered = result.text.lower()
    for part in _user_parts(user):
        assert part.lower() not in lowered, (part, result.text)
    assert "'" not in result.text, result.text
    assert "host" not in lowered.replace("[redacted:unc_host]", ""), result.text  # UNC/URL hosts go too
    assert result.counts.get("home_path", 0) >= 1, result.counts
    assert result.text.startswith("The operator keeps the build at ") and result.text.endswith(" for the release.")


def test_a_home_path_in_brackets_or_backticks_keeps_its_closing_delimiter():
    assert prepare_egress("[/home/alice]").text == "[[REDACTED:home_path]]"
    assert prepare_egress("run `/home/alice/x.sh`").text == "run `[REDACTED:home_path]/x.sh`"
    assert prepare_egress("cd `/home/alice`").text == "cd `[REDACTED:home_path]`"
    assert prepare_egress("{C:" + BS + "Users" + BS + "alice}").text == "{[REDACTED:home_path]}"


def test_percent_encoded_email_is_redacted():
    for text in ("alice%40example.com", "mailto:alice%40example.com", "to=alice%2540example.com"):
        result = prepare_egress(f"send it to {text} today")
        assert "alice" not in result.text and "example.com" not in result.text, result.text
        assert result.counts.get("email") == 1, result.counts


LABELLED_PHONES = [
    "tel:+5491112345678",
    "tel:+54-9-11-1234-5678",
    "WhatsApp:+5491145678901",
    "[llamar](tel:+5491145678901)",
    "phone:+54911 1234 5678",
    "Cel:11-1234-5678",
    "tel:011-4567-8901",
    "tel.011 4567-8901",
    "cel.11-1234-5678",
    "phone/+5491145678901",
    "Tel.: (011) 4567-8901",
    "x.+5491145678901",
    "https://wa.me/5491145678901",
    "https://api.whatsapp.com/send?phone=5491145678901&text=hola",
]


@pytest.mark.parametrize("text", LABELLED_PHONES)
def test_phone_numbers_after_a_label_or_in_a_tel_uri_are_redacted(text):
    result = prepare_egress(f"Contact {text} today.")
    digits = "".join(c for c in text if c.isdigit())
    assert digits[-4:] not in result.text and digits[-8:-4] not in result.text, result.text
    assert result.counts.get("phone") == 1, result.counts


SPACED_PHONES = ["11 1234 5678", "011 4567 8901", "(011) 4567 8901", "0351 4567890", "351 456 7890",
                 "11 15 1234 5678"]


@pytest.mark.parametrize("phone", SPACED_PHONES)
def test_argentine_numbers_separated_only_by_spaces_are_redacted(phone):
    result = prepare_egress(f"Call the operator at {phone} before deploying.")
    assert result.text == "Call the operator at [REDACTED:phone] before deploying.", result.text


PHONE_LOOKALIKES = [
    "took 12 1200-1300 ms", "lines 120 1200-1300", "id 2024-0001-2345", "SHA 1234-5678-9012",
    "claims 1234 5678-9012", "range 2024 2025-2026", "Q3 2026 1000-2000", "(2026) 123-4567",
    "score: +0.12345678", "version:1.13.0 and v4.9.0:2026-09-23", "at 10:30:00.123+0300",
    "http://localhost:8080/1234-5678", "commit:6909841 claim:678901 port:8443",
    "sizes 1200 3400 5600 and 2024 1234 5678",
]


@pytest.mark.parametrize("text", PHONE_LOOKALIKES)
def test_number_runs_that_are_not_phones_are_left_alone(text):
    result = prepare_egress(text)
    assert result.text == text, result.text
    assert "phone" not in result.counts


# --- wave 3 verifier round 2: hidden home shares, WSL-to-Windows, MSYS/Cygwin, macOS volumes,
# rooted \Users, and the phone spellings the first pass missed ---------------------------------

MORE_HOME_PATHS = {
    "unc_hidden_users_share": BS * 2 + "host" + BS + "users$" + BS + "{u}" + BS + "Desktop",
    "unc_hidden_home_share": BS * 2 + "host" + BS + "home$" + BS + "{u}" + BS + "x",
    "unc_hidden_json_escaped": BS * 4 + "host" + BS * 2 + "home$" + BS * 2 + "{u}" + BS * 2 + "x",
    "slash_unc_hidden_home": "//host/home$/{u}/x",
    "smb_hidden_home": "smb://host/home$/{u}/x",
    "smb_hidden_users": "smb://host/Users$/{u}/x",
    "wsl_windows_mount": BS * 2 + "wsl$" + BS + "Ubuntu" + BS + "mnt" + BS + "c" + BS + "Users" + BS + "{u}" + BS + "x",
    "wsl_slash_windows_mount": "//wsl$/Ubuntu/mnt/c/Users/{u}/x",
    "wsl_localhost_windows_mount": BS * 2 + "wsl.localhost" + BS + "Ubuntu" + BS + "mnt" + BS + "c" + BS + "Users"
    + BS + "{u}" + BS + "x",
    "msys_home_backslash": "C:" + BS + "msys64" + BS + "home" + BS + "{u}" + BS + "x",
    "msys_home_forward": "C:/msys64/home/{u}/x",
    "cygwin_home_forward": "C:/cygwin64/home/{u}/x",
    "cygwin_home_backslash": "D:" + BS + "cygwin" + BS + "home" + BS + "{u}" + BS + "x",
    "mac_volume_users": "/Volumes/Data/Users/{u}/x",
    "mac_volume_spaced_disk": "/Volumes/Macintosh HD/Users/{u}/x",
    "rooted_users": BS + "Users" + BS + "{u}" + BS + "x",
    "rooted_users_json_escaped": BS * 2 + "Users" + BS * 2 + "{u}" + BS * 2 + "x",
}


@pytest.mark.parametrize("user", TRICKY_USERS)
@pytest.mark.parametrize("name", sorted(MORE_HOME_PATHS))
def test_more_home_path_spellings_hide_the_whole_user_name(name, user):
    text = f"The operator keeps the build at {MORE_HOME_PATHS[name].format(u=user)} for the release."
    result = prepare_egress(text)
    assert not result.blocked, result.reason
    lowered = result.text.lower()
    for part in _user_parts(user):
        assert part.lower() not in lowered, (part, result.text)
    assert "'" not in result.text, result.text
    assert "host" not in lowered.replace("[redacted:unc_host]", ""), result.text
    assert result.counts.get("home_path", 0) >= 1, result.counts
    assert result.text.startswith("The operator keeps the build at ") and result.text.endswith(" for the release.")


MORE_HOME_BENIGN = [
    "The share " + BS * 2 + "nas01" + BS + "public$" + BS + "installers holds the ISO.",
    "Mounted /Volumes/Backup/Photos and /Volumes/Data/Shared/x read-only.",
    "Build with C:/msys64/usr/bin/bash.exe and C:" + BS + "cygwin64" + BS + "bin" + BS + "ls.exe",
    "The API route " + BS + "users" + BS + "{id} is not a home.",
]


@pytest.mark.parametrize("text", MORE_HOME_BENIGN)
def test_more_home_path_rules_leave_ordinary_text_alone(text):
    result = prepare_egress(text)
    assert "home_path" not in result.counts, (result.counts, result.text)
    assert "installers" in result.text or "Photos" in result.text or "bash.exe" in result.text \
        or "{id}" in result.text


MORE_PHONES = [
    "tel:%2B5491112345678",
    "tel:%2B54%209%2011%201234-5678",
    "15-1234-5678",
    "15-456-7890",
    "02944 45-6789",
    "(02944) 456789",
    "(02944) 45-6789",
]


@pytest.mark.parametrize("phone", MORE_PHONES)
def test_more_argentine_and_encoded_phones_are_redacted(phone):
    result = prepare_egress(f"Call the operator at {phone} before deploying.")
    digits = "".join(c for c in urllib.parse.unquote(phone) if c.isdigit())
    assert digits[-4:] not in result.text, result.text
    assert result.text == "Call the operator at [REDACTED:phone] before deploying.", result.text
    assert result.counts.get("phone") == 1, result.counts


MORE_PHONE_LOOKALIKES = [
    "retries 15 1200 3000 ms", "(0351) 45-678 and 0351 45-6789", "range 15-20 and 15-2026",
    "percent 15%25 of 1234-5678-abc", "q=100%25&x=2026-09-23",
]


@pytest.mark.parametrize("text", MORE_PHONE_LOOKALIKES)
def test_more_number_runs_that_are_not_phones_are_left_alone(text):
    result = prepare_egress(text)
    assert result.text == text, result.text
    assert "phone" not in result.counts


# --- docs/jev-decisions.md states exactly what is covered: every home-path spelling the doc
# lists (before its "Not covered:" line) is run through the redactor with every tricky user.

_DOC_PLACEHOLDERS = (
    ("http(s)://<host>[:<port>]", "https://files.example.com:8443"),
    (".../", "https://files.example.com/"),
    ("<host>", "nas01"), ("<nas>", "nas01"), ("<distro>", "Ubuntu"), ("<drive>", "c"),
    ("<disk>", "Macintosh HD"), ("<share>", "data"), ("<N>", "1"), ("<port>", "8443"),
)


def _documented_home_spellings() -> list[str]:
    import re
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "jev-decisions.md").read_text(encoding="utf-8")
    start = doc.index("- **Home directories** (`home_path`)")
    section = doc[start:doc.index("  Not covered:", start)]
    spellings = []
    for span in re.findall(r"`([^`]+)`", section):
        if "<u>" not in span:
            continue
        for placeholder, value in _DOC_PLACEHOLDERS:
            span = span.replace(placeholder, value)
        spellings.append(span)
    return spellings


def test_the_doc_lists_the_home_path_spellings_it_claims():
    spellings = _documented_home_spellings()
    assert len(spellings) >= 40, spellings
    assert "~<u>".replace("<u>", "{u}") in [s.replace("<u>", "{u}") for s in spellings]
    assert BS * 2 + "wsl$" + BS + "Ubuntu" + BS + "home" + BS + "<u>" in spellings
    assert BS * 2 + "nas01" + BS + "home" + BS + "<u>" in spellings  # two leading backslashes, as typed


@pytest.mark.parametrize("user", TRICKY_USERS)
def test_every_documented_home_path_spelling_hides_the_whole_user_name(user):
    for spelling in _documented_home_spellings():
        text = f"The operator keeps the build at {spelling.replace('<u>', user)} for the release."
        result = prepare_egress(text)
        assert not result.blocked, (spelling, result.reason)
        lowered = result.text.lower()
        for part in _user_parts(user):
            assert part.lower() not in lowered, (spelling, part, result.text)
        assert "'" not in result.text, (spelling, result.text)
        assert result.counts.get("home_path", 0) >= 1, (spelling, result.counts)
