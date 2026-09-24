"""Ruling R3 (4.9.0): every export redactor covers the remaining home-path spellings.

WSL mounts of a Windows profile (``/mnt/<drive>/Users/<u>/...``, any case, user
names with spaces), lowercase ``/users/<u>`` and ``~user`` must not leave through
the Jev egress (``decisions.egress``), the compiled-profile map provider
(``profile.egress``) or the Auto Dream export (``bridges.dream_bridge``, which
refuses rather than rewrites).  Prose tildes, ``~/``, public URLs, REST
placeholders and dates stay untouched.
"""
from __future__ import annotations

import pytest

from memorymaster.bridges.dream_bridge import _is_sensitive
from memorymaster.decisions.egress import prepare_egress
from memorymaster.profile.egress import prepare_claim_egress

HOME_PATHS = {
    "wsl_mount": "/mnt/c/Users/alice/repo/notes.md",
    "wsl_mount_other_drive": "/mnt/d/Users/alice/repo",
    "wsl_mount_upper": "/MNT/C/USERS/alice/repo",
    "wsl_mount_spaced_user": "/mnt/c/Users/Alice Smith/repo/notes.md",
    "lowercase_users": "/users/alice/repo/notes.md",
    "lowercase_users_spaced_user": "/users/Alice Smith/repo/notes.md",
    "upper_users": "/USERS/alice/repo",
    "gitbash_lowercase_users": "/c/users/alice/repo",
    "windows_lowercase": r"c:\users\alice\repo",
    "wsl_unc_home": r"\\wsl$\Ubuntu\home\alice\repo",
    "tilde_user_path": "~alice/repo/notes.md",
    "tilde_user_bare": "~alice",
}
SAFE = (
    "It takes ~5 minutes; notes live in ~/notes and ~/.bashrc.",
    "Docs at https://example.com/users/guide and https://example.com/c/users/x",
    "The API route GET /users/:id and /users/{id} returns the profile.",
    "Released 2026-09-23T10:00:00Z as version 4.9.0.",
)


def _sentence(path: str) -> str:
    return f"The operator keeps the build scripts at {path} for the release."


def _leaks(text: str) -> bool:
    lowered = text.lower()
    return "alice" in lowered or "smith" in lowered


@pytest.mark.parametrize("name", sorted(HOME_PATHS))
def test_jev_egress_redacts_every_home_path_spelling(name):
    result = prepare_egress(_sentence(HOME_PATHS[name]))

    assert not result.blocked
    assert not _leaks(result.text), result.text
    assert result.counts.get("home_path", 0) >= 1


@pytest.mark.parametrize("name", sorted(HOME_PATHS))
def test_profile_egress_redacts_every_home_path_spelling(name):
    result = prepare_claim_egress(_sentence(HOME_PATHS[name]))

    assert not result.blocked
    assert not _leaks(result.text), result.text
    # Backslash spellings go whole through the absolute Windows/UNC path rules.
    assert {"home_path", "absolute_path_windows", "absolute_path_unc"} & set(result.findings), result.findings


# A bare ``~alice`` is redacted on egress, but the Dream bridge refuses whole
# memories on import AND export and cannot tell it from prose (``takes ~ten
# minutes``); it refuses the home-path form ``~alice/...`` only.
@pytest.mark.parametrize("name", sorted(set(HOME_PATHS) - {"tilde_user_bare"}))
def test_dream_export_refuses_every_home_path_spelling(name):
    assert _is_sensitive(_sentence(HOME_PATHS[name])) is True


@pytest.mark.parametrize("text", [
    "Rebuilding the index takes ~ten minutes on the laptop.",
    "Scores stay ~approx and turn ~stale after a week without use.",
    "GET /users/me returns the caller; /users/42 and /users/admin return other accounts.",
])
def test_dream_bridge_keeps_prose_tildes_and_rest_routes(text):
    # These memories were refused on import and export (6 of 417 local files).
    assert _is_sensitive(text) is False


def test_dream_bridge_still_refuses_a_macos_home_named_like_a_route():
    assert _is_sensitive("Scripts live in /Users/admin/repo on the mac mini.") is True


@pytest.mark.parametrize("path", ["/mnt/c/Users/alice", "/Users/alice", "/users/alice", r"C:\Users\alice"])
def test_a_closing_parenthesis_after_a_home_path_is_kept(path):
    for redacted in (prepare_egress(f"see ({path}) now").text, prepare_claim_egress(f"see ({path}) now").text):
        assert redacted.endswith(") now"), redacted
        assert "alice" not in redacted.lower()


def test_dream_export_refuses_forward_slash_windows_profiles():
    assert _is_sensitive("Logs are under C:/Users/alice/AppData for the hook.") is True


@pytest.mark.parametrize("text", SAFE)
def test_ordinary_text_is_left_alone(text):
    assert prepare_egress(text).text == text
    assert prepare_claim_egress(text).text == text
    assert _is_sensitive(text) is False
