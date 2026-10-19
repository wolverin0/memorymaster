"""T-1591: ``infer_structure`` must not turn loopback addresses or docs URLs into facts.

It used to stamp ``server / ip_address / <first IPv4>`` and ``service / url /
<first URL>`` on any prose that mentioned an address, so long legitimate claims
that only cited 127.0.0.1, localhost or a docs page carried a bogus triple and
were auto-confirmed with it. Real infra addresses (including private ones) must
still be inferred.
"""
from __future__ import annotations

import pytest

from memorymaster.govern.jobs.extractor import infer_structure

NO_TRIPLE = (None, None, None, None)


@pytest.mark.parametrize("text", [
    "Dev server listens on 127.0.0.1 only",
    "Bound to 127.0.0.1:8080 for local dev",
    "Loopback range 127.4.5.6 is used by the sidecar",
    "Open http://localhost:3003/health to check",
    "Probe http://127.0.0.1:8000/api/v1/items first",
    "Health at https://localhost/ready",
    "IPv6 http://[::1]:8080/ready works too",
    "Read https://code.claude.com/docs/en/sub-agents#choose-a-model for model resolution",
    "See https://code.claude.com for the CLI reference",
    "Docs https://sub.code.claude.com/page",
])
def test_loopback_and_docs_urls_get_no_triple(text):
    assert infer_structure(text) == NO_TRIPLE


@pytest.mark.parametrize("text,expected", [
    ("Prod host is 10.0.0.5", ("infra_fact", "server", "ip_address", "10.0.0.5")),
    ("Public edge 203.0.113.9 fronts the app", ("infra_fact", "server", "ip_address", "203.0.113.9")),
    ("NAS at 192.168.1.20 (admin UI)", ("infra_fact", "server", "ip_address", "192.168.1.20")),
    ("Staging at https://staging.example.com/health",
     ("infra_fact", "service", "url", "https://staging.example.com/health")),
    ("Docs at https://notcode.claude.com.evil.test/x",
     ("infra_fact", "service", "url", "https://notcode.claude.com.evil.test/x")),
])
def test_real_infra_facts_are_still_inferred(text, expected):
    assert infer_structure(text) == expected


def test_a_real_address_after_a_loopback_one_is_still_found():
    assert infer_structure("Local 127.0.0.1 proxies to the host 10.0.0.5") == (
        "infra_fact", "server", "ip_address", "10.0.0.5",
    )


def test_a_real_url_after_a_loopback_or_docs_one_is_still_found():
    text = "See https://code.claude.com/docs and http://localhost:3003 then https://api.example.com/v1"
    assert infer_structure(text) == ("infra_fact", "service", "url", "https://api.example.com/v1")


def test_loopback_ip_does_not_shadow_the_url_branch():
    assert infer_structure("Run on 127.0.0.1 then call https://api.example.com/v1") == (
        "infra_fact", "service", "url", "https://api.example.com/v1",
    )


def test_other_branches_are_unchanged():
    assert infer_structure(r"Config lives in C:\work\app\conf.ini")[0] == "filesystem_fact"
    assert infer_structure("The secret is stored in the vault")[:3] == ("security_fact", "auth", "location_hint")
    assert infer_structure("Plain prose without anything structured") == NO_TRIPLE
