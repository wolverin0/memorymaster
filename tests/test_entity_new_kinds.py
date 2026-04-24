"""Unit tests for Layer-1 kinds added in roadmap item 3.2.

New kinds: ``package``, ``url_domain``, ``slash_command``, ``claim_id_ref``.

Each test asserts:
  * the kind is emitted,
  * the canonical_hint is correctly normalized,
  * dedup behaves as documented (same (kind, canonical_hint) only once).

These tests are additive — they do NOT re-assert the pre-existing kinds
(covered in ``tests/test_entity_extractor.py``), only the new ones.
"""
from __future__ import annotations

from memorymaster.entity_extractor import Entity, extract_patterns


def _of_kind(entities: list[Entity], kind: str) -> list[Entity]:
    return [e for e in entities if e.kind == kind]


def _canonicals(entities: list[Entity], kind: str) -> set[str]:
    return {e.canonical_hint for e in _of_kind(entities, kind)}


# -- package ---------------------------------------------------------------


def test_package_pip_install_single() -> None:
    out = extract_patterns("Run `pip install fastmcp` to get the server.")
    assert _canonicals(out, "package") == {"fastmcp"}


def test_package_pip_install_multi_and_qdrant() -> None:
    out = extract_patterns("pip install qdrant-client sentence-transformers scikit-learn")
    canonicals = _canonicals(out, "package")
    # qdrant-client is already canonical; scikit-learn stays; PEP-503 collapse
    # means ``sentence_transformers`` would have matched ``sentence-transformers``.
    assert "qdrant-client" in canonicals
    assert "sentence-transformers" in canonicals
    assert "scikit-learn" in canonicals


def test_package_python_import_statement() -> None:
    out = extract_patterns("import numpy\nimport pandas as pd")
    canonicals = _canonicals(out, "package")
    assert "numpy" in canonicals
    assert "pandas" in canonicals


def test_package_from_import() -> None:
    out = extract_patterns("from fastapi import APIRouter")
    assert "fastapi" in _canonicals(out, "package")


def test_package_node_install() -> None:
    out = extract_patterns("npm install react\npnpm add zustand")
    canonicals = _canonicals(out, "package")
    assert "react" in canonicals
    assert "zustand" in canonicals


def test_package_canonicalization_underscores() -> None:
    # ``scikit_learn`` and ``scikit-learn`` must collapse to the same canonical
    # name per PEP 503.
    out = extract_patterns(
        "pip install scikit_learn\npip install scikit-learn"
    )
    assert _canonicals(out, "package") == {"scikit-learn"}


def test_package_ignores_flags_and_stopwords() -> None:
    # Extractor is strict-contiguous: once an English stopword (``the``)
    # appears after the flag, the run terminates. This is INTENTIONAL —
    # the word ``numpy`` buried deep in prose after ``pip install`` is not
    # reliable signal. The test asserts none of the noise words land; it
    # does NOT demand the final ``numpy`` token be rescued because doing
    # so would require resuming after an unrelated English phrase.
    out = extract_patterns("pip install --upgrade the package with numpy")
    canonicals = _canonicals(out, "package")
    assert "the" not in canonicals
    assert "with" not in canonicals
    assert "install" not in canonicals


def test_package_flag_then_package_still_caught() -> None:
    # ``pip install --upgrade numpy`` — flag is skipped, numpy is
    # immediately adjacent after flag-whitespace, so the run grabs it.
    out = extract_patterns("pip install --upgrade numpy pandas")
    canonicals = _canonicals(out, "package")
    assert "numpy" in canonicals
    assert "pandas" in canonicals


def test_package_no_false_positive_in_plain_prose() -> None:
    # Text without an import/install verb should yield zero packages — avoids
    # harvesting every lowercase word in prose.
    out = extract_patterns("The service cache latency looked fine.")
    assert _canonicals(out, "package") == set()


# -- url_domain ------------------------------------------------------------


def test_url_domain_basic() -> None:
    out = extract_patterns("See https://github.com/foo/bar for the PR.")
    assert _canonicals(out, "url_domain") == {"github.com"}


def test_url_domain_strips_www() -> None:
    out = extract_patterns("Docs at https://www.anthropic.com/claude.")
    assert _canonicals(out, "url_domain") == {"anthropic.com"}


def test_url_domain_case_insensitive() -> None:
    out = extract_patterns("Mixed case https://GitHub.com/foo is common.")
    assert _canonicals(out, "url_domain") == {"github.com"}


def test_url_domain_handles_port_and_path() -> None:
    out = extract_patterns(
        "Healthcheck http://grafana.internal:3000/d/abc/dashboard"
    )
    assert _canonicals(out, "url_domain") == {"grafana.internal"}


def test_url_domain_dedup_same_host_multiple_urls() -> None:
    out = extract_patterns(
        "https://api.anthropic.com/v1/messages and https://api.anthropic.com/v1/complete"
    )
    # dedup on (kind, canonical_hint)
    hosts = _of_kind(out, "url_domain")
    assert len(hosts) == 1
    assert hosts[0].canonical_hint == "api.anthropic.com"


def test_url_domain_requires_tld() -> None:
    # Bare ``http://localhost`` has no TLD; the regex requires ``.xx`` so it
    # should not match.
    out = extract_patterns("Server running on http://localhost:8080")
    assert _canonicals(out, "url_domain") == set()


# -- slash_command ---------------------------------------------------------


def test_slash_command_basic() -> None:
    out = extract_patterns("Run /wiki to open the panel.")
    assert _canonicals(out, "slash_command") == {"/wiki"}


def test_slash_command_with_namespace() -> None:
    out = extract_patterns("Invoke /superpowers:brainstorming before coding.")
    assert "/superpowers:brainstorming" in _canonicals(out, "slash_command")


def test_slash_command_multiple() -> None:
    out = extract_patterns("Use /graphify, /autoresearch, and /wiki together.")
    canonicals = _canonicals(out, "slash_command")
    assert canonicals == {"/graphify", "/autoresearch", "/wiki"}


def test_slash_command_rejects_posix_paths() -> None:
    # These must NOT be treated as slash commands.
    out = extract_patterns(
        "Binary at /usr/bin/foo and logs at /var/log/app.log"
    )
    assert _canonicals(out, "slash_command") == set()


def test_slash_command_dedup() -> None:
    out = extract_patterns("/wiki and /wiki again and /wiki")
    cmds = _of_kind(out, "slash_command")
    assert len(cmds) == 1


def test_slash_command_not_inside_url() -> None:
    # ``/messages`` inside a URL should not count — it's a URL path segment.
    out = extract_patterns("See https://api.example.com/messages for details.")
    assert _canonicals(out, "slash_command") == set()


# -- claim_id_ref ----------------------------------------------------------


def test_claim_id_ref_numeric_singular() -> None:
    out = extract_patterns("See claim 11822 for worktree isolation.")
    assert _canonicals(out, "claim_id_ref") == {"claim_11822"}


def test_claim_id_ref_numeric_plural() -> None:
    out = extract_patterns("Claims 11825 and claim 11847 apply here.")
    canonicals = _canonicals(out, "claim_id_ref")
    assert "claim_11825" in canonicals
    assert "claim_11847" in canonicals


def test_claim_id_ref_mm_prefix() -> None:
    out = extract_patterns("Superseded by mm-abcd1234 in last cycle.")
    assert _canonicals(out, "claim_id_ref") == {"mm-abcd1234"}


def test_claim_id_ref_mm_with_version_suffix() -> None:
    out = extract_patterns("The mm-abcd1234~0 variant is the original.")
    assert "mm-abcd1234~0" in _canonicals(out, "claim_id_ref")


def test_claim_id_ref_no_false_positive_on_bare_number() -> None:
    # ``11822`` alone is not a claim reference — requires the keyword.
    out = extract_patterns("The number 11822 was picked arbitrarily.")
    assert _canonicals(out, "claim_id_ref") == set()


def test_claim_id_ref_dedup() -> None:
    out = extract_patterns("claim 11822 and claim 11822 and claim 11822")
    refs = _of_kind(out, "claim_id_ref")
    assert len(refs) == 1


# -- integration -----------------------------------------------------------


def test_all_new_kinds_coexist_in_one_text() -> None:
    text = (
        "To ship the fix, pip install fastmcp and then run /wiki absorb. "
        "See https://github.com/memorymaster/wiki for claim 11889."
    )
    out = extract_patterns(text)
    kinds = {e.kind for e in out}
    assert "package" in kinds
    assert "url_domain" in kinds
    assert "slash_command" in kinds
    assert "claim_id_ref" in kinds


def test_returns_entity_dataclass() -> None:
    out = extract_patterns("pip install fastmcp\nrun /wiki")
    assert out
    assert all(isinstance(e, Entity) for e in out)
    # canonical_hint is always a non-empty string
    assert all(e.canonical_hint for e in out)
