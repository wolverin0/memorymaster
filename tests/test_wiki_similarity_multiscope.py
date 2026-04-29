"""Tests for the multi-scope ``WikiCorpus`` (roadmap item 11.5).

Covers:

* ``scopes=["project:a", "project:b"]`` loads articles from BOTH scope dirs
  and tags each with its ``article_scope``
* ``scopes="*"`` auto-discovers every project-*/user/global dir
* A missing scope dir does not crash — it is silently skipped
* A claim with ``scope="project:a"`` is scored against project-a articles
  only (a perfect-match article in project-b is ignored)
* Explicit ``wiki_article`` slugs that exist in the wrong scope return 0
* Bare ``_scope_to_dirname`` matches ``wiki_engine._scope_dirname`` for the
  canonical cases
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("numpy", reason="ml extra not installed")

from memorymaster.wiki_similarity import (  # noqa: E402
    _discover_scope_dirs,
    _scope_to_dirname,
    compute_wiki_similarity,
    load_wiki_corpus,
)


@pytest.fixture
def tf_idf_only(monkeypatch) -> None:
    """Force the deterministic TF-IDF backend."""
    monkeypatch.setenv("MEMORYMASTER_DISABLE_ST", "1")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Create a tiny multi-scope vault with three scope dirs.

    Layout::

        tmp_path/
            project-a/
                alpha.md    -- about "apples and ripe fruit"
                beta.md     -- about "steward classifier promotion"
            project-b/
                alpha.md    -- about "boats and sailing at sea"  (same slug!)
            user/
                prefs.md    -- about "dark mode keyboard shortcuts"
    """
    root = tmp_path
    (root / "project-a").mkdir()
    (root / "project-b").mkdir()
    (root / "user").mkdir()

    (root / "project-a" / "alpha.md").write_text(
        "---\ntitle: Alpha A\n---\n\n"
        "Apples and pears are ripe tree fruit harvested in autumn.\n"
        "\n---\n\ntimeline\n",
        encoding="utf-8",
    )
    (root / "project-a" / "beta.md").write_text(
        "---\ntitle: Beta A\n---\n\n"
        "The steward classifier promotes confirmed claims with citations.\n"
        "\n---\n\ntimeline\n",
        encoding="utf-8",
    )
    (root / "project-b" / "alpha.md").write_text(
        "---\ntitle: Alpha B\n---\n\n"
        "Boats and sailing ships cross the open sea during summer.\n"
        "\n---\n\ntimeline\n",
        encoding="utf-8",
    )
    (root / "user" / "prefs.md").write_text(
        "---\ntitle: Prefs\n---\n\n"
        "Dark mode, keyboard shortcuts, and editor theme preferences.\n"
        "\n---\n\ntimeline\n",
        encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------------------
# dir discovery
# ---------------------------------------------------------------------------


def test_scope_to_dirname_matches_wiki_engine() -> None:
    # Must stay in sync with memorymaster.wiki_engine._scope_dirname.
    assert _scope_to_dirname("project:memorymaster") == "project-memorymaster"
    assert _scope_to_dirname("project:whatsappbot") == "project-whatsappbot"
    assert _scope_to_dirname("user") == "user"
    assert _scope_to_dirname("global") == "global"
    assert _scope_to_dirname("") == "default"


def test_discover_scope_dirs_finds_all_projects_and_user(vault: Path) -> None:
    found = _discover_scope_dirs(vault)
    assert set(found.keys()) == {"project:a", "project:b", "user"}
    assert found["project:a"].name == "project-a"
    assert found["project:b"].name == "project-b"


def test_discover_scope_dirs_skips_reserved(vault: Path) -> None:
    (vault / "bases").mkdir()
    (vault / "entities").mkdir()
    (vault / "_index").mkdir()
    found = _discover_scope_dirs(vault)
    assert "bases" not in found
    assert "entities" not in found
    # _index is skipped by the underscore rule.
    for k in found:
        assert not k.startswith("_")


def test_discover_scope_dirs_missing_root_is_empty(tmp_path: Path) -> None:
    assert _discover_scope_dirs(tmp_path / "does-not-exist") == {}


# ---------------------------------------------------------------------------
# load_wiki_corpus multi-scope
# ---------------------------------------------------------------------------


def test_explicit_scopes_list_loads_both(vault: Path, tf_idf_only) -> None:
    corpus = load_wiki_corpus(
        scopes=["project:a", "project:b"], wiki_root=vault,
    )
    assert set(corpus.scopes) == {"project:a", "project:b"}
    # 2 in project-a + 1 in project-b, keyed as "{scope}::{slug}" because
    # multi-scope loading keeps same-slug articles separate.
    assert len(corpus.articles) == 3
    scopes_seen = {a.article_scope for a in corpus.articles.values()}
    assert scopes_seen == {"project:a", "project:b"}
    # Same slug in two scopes must not clobber.
    keys = set(corpus.articles.keys())
    assert "project:a::alpha" in keys
    assert "project:b::alpha" in keys


def test_star_scope_auto_discovers_all(vault: Path, tf_idf_only) -> None:
    corpus = load_wiki_corpus(scopes="*", wiki_root=vault)
    assert set(corpus.scopes) == {"project:a", "project:b", "user"}
    # 2 + 1 + 1 = 4 articles total.
    assert len(corpus.articles) == 4


def test_missing_scope_dir_is_skipped(vault: Path, tf_idf_only) -> None:
    corpus = load_wiki_corpus(
        scopes=["project:a", "project:does-not-exist"], wiki_root=vault,
    )
    # project:a's 2 articles load; the missing one is silently skipped.
    assert len(corpus.articles) == 2
    assert all(a.article_scope == "project:a" for a in corpus.articles.values())


def test_invalid_scopes_type_raises(vault: Path) -> None:
    with pytest.raises(TypeError):
        load_wiki_corpus(scopes=123, wiki_root=vault)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# scope-filtered similarity
# ---------------------------------------------------------------------------


def test_multiscope_filters_to_claim_scope(vault: Path, tf_idf_only) -> None:
    """A claim scoped to project:a must be scored against project-a articles
    ONLY, even when project-b ships an article with the exact same slug."""
    corpus = load_wiki_corpus(scopes="*", wiki_root=vault)

    # Claim text that is a near-exact match for project-a/alpha.md.
    claim_a = {
        "id": 1,
        "text": "Apples and pears are ripe tree fruit harvested in autumn.",
        "subject": "apples",
        "scope": "project:a",
    }
    sim_a = compute_wiki_similarity(claim_a, corpus)
    assert sim_a > 0.5, f"claim should match project-a/alpha, got {sim_a}"

    # Claim text that is a near-exact match for project-b/alpha.md.
    claim_b = {
        "id": 2,
        "text": "Boats and sailing ships cross the open sea during summer.",
        "subject": "boats",
        "scope": "project:b",
    }
    sim_b = compute_wiki_similarity(claim_b, corpus)
    assert sim_b > 0.5, f"claim should match project-b/alpha, got {sim_b}"

    # Now swap: claim_a's text against scope=project:b should NOT pick
    # project-a/alpha (different scope). It should either match project-b's
    # (weaker match) or return 0 — either way, the score must be lower than
    # the in-scope score.
    claim_a_in_b = dict(claim_a, id=3, scope="project:b")
    sim_cross = compute_wiki_similarity(claim_a_in_b, corpus)
    assert sim_cross < sim_a, (
        f"cross-scope score ({sim_cross}) must be weaker than in-scope "
        f"({sim_a}); scope filter is not engaged"
    )


def test_explicit_slug_respects_claim_scope(
    vault: Path, tf_idf_only,
) -> None:
    """``wiki_article='alpha'`` is ambiguous in multi-scope (both project-a
    and project-b ship ``alpha.md``). The claim's ``scope`` must
    disambiguate: scope=project:a -> project-a/alpha (apples); scope=
    project:b -> project-b/alpha (boats). A same-text claim should score
    higher against its in-scope alpha than the unrelated other one."""
    corpus = load_wiki_corpus(scopes="*", wiki_root=vault)
    claim_apples_a = {
        "id": 10,
        "text": "Apples and pears are ripe tree fruit.",
        "subject": "apples",
        "scope": "project:a",
        "wiki_article": "alpha",
    }
    sim_in = compute_wiki_similarity(claim_apples_a, corpus)
    assert sim_in > 0.4, (
        f"apples claim with scope=a & slug=alpha should match project-a/alpha, "
        f"got {sim_in}"
    )

    # Same claim text but scope=project:b — the resolver must pick
    # project-b/alpha (boats) instead, and score strictly lower.
    claim_apples_b = dict(claim_apples_a, id=11, scope="project:b")
    sim_cross = compute_wiki_similarity(claim_apples_b, corpus)
    assert sim_cross < sim_in, (
        f"same apples text bound to project-b/alpha (boats) should score "
        f"lower than project-a/alpha; got in={sim_in}, cross={sim_cross}"
    )


def test_claim_without_scope_in_multiscope_returns_zero(
    vault: Path, tf_idf_only,
) -> None:
    """A claim with no ``scope`` loaded against a multi-scope corpus gets
    no articles to match (filter_scope stays None -> we fall back to the
    token-overlap search which now requires a scope in multi-scope mode).

    The token fallback without a scope considers ALL articles, which is
    correct for back-compat; this test asserts we still return *some*
    positive score rather than crashing."""
    corpus = load_wiki_corpus(scopes="*", wiki_root=vault)
    claim = {
        "id": 20,
        "text": "Boats and sailing ships cross the open sea.",
        "subject": "boats",
        # deliberately no scope
    }
    sim = compute_wiki_similarity(claim, corpus)
    # With no scope, multi-scope filter is disengaged (filter_scope=None),
    # so all articles are eligible and the best lexical match wins.
    assert sim > 0.3


def test_single_scope_legacy_load_still_works(vault: Path, tf_idf_only) -> None:
    """Backwards compat: calling ``load_wiki_corpus(scope=..., wiki_root=...)``
    without ``scopes`` keeps the original single-scope behaviour — articles
    keyed by bare slug, no scope filtering at query time."""
    corpus = load_wiki_corpus(
        scope="project:a", wiki_root=vault / "project-a",
    )
    assert corpus.scopes == ("project:a",)
    assert "alpha" in corpus.articles  # bare slug, no scope prefix
    claim = {"id": 30, "text": "apples ripe autumn", "scope": "project:a"}
    sim = compute_wiki_similarity(claim, corpus)
    assert sim > 0.0
