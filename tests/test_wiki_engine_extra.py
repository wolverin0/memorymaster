"""Coverage hardening for memorymaster.wiki_engine.

These tests stub the LLM so the absorb / cleanup / breakdown / single-claim
paths run end-to-end without any network or provider, and assert the *intent*
of each branch:

- absorb CREATE writes an article whose frontmatter carries the LLM body
  (proves the create branch runs and the >50 char guard is honoured).
- absorb UPDATE preserves the existing timeline while rewriting compiled
  truth (this is the whole point of the two-section wiki format — a regression
  here silently destroys append-only evidence).
- absorb_single_claim creates then updates a single subject article.
- cleanup rewrites only low-scoring articles and keeps frontmatter.
- breakdown surfaces missing entities and delegates creation to absorb.
- frontmatter helpers (explored preservation, description extraction, tag
  derivation, yaml escaping) encode the schema invariants enforced by the
  validate-wiki hook.

The empty-LLM path is also covered: when ``_call_llm`` returns "" no article
is written, which is the safety contract that stops MemoryMaster from emitting
blank wiki pages on provider outage.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memorymaster import wiki_engine


# Columns drawn from every SELECT in wiki_engine.py plus the binding column.
_CLAIMS_DDL = """
CREATE TABLE claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT,
    claim_type TEXT,
    subject TEXT,
    predicate TEXT,
    object_value TEXT,
    scope TEXT,
    confidence REAL,
    status TEXT,
    human_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    event_time TEXT,
    valid_from TEXT,
    wiki_article TEXT
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    created_at TEXT
);
"""


def _fresh_db(path: Path) -> str:
    conn = sqlite3.connect(path)
    conn.executescript(_CLAIMS_DDL)
    conn.commit()
    conn.close()
    return str(path)


def _seed(db: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(db)
    for r in rows:
        conn.execute(
            """INSERT INTO claims
               (text, claim_type, subject, predicate, object_value, scope,
                confidence, status, human_id, created_at, updated_at, event_time)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                r["text"],
                r.get("claim_type", "fact"),
                r.get("subject"),
                r.get("predicate", ""),
                r.get("object_value", ""),
                r.get("scope", "project:demo"),
                r.get("confidence", 0.9),
                r.get("status", "confirmed"),
                r.get("human_id", "mm-test"),
                r.get("created_at", "2026-01-01T00:00:00Z"),
                r.get("updated_at", "2026-01-02T00:00:00Z"),
                r.get("event_time", "2026-01-01"),
            ),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def stub_llm(monkeypatch):
    """Replace the provider layer so wiki_engine._call_llm returns canned text.

    Patches llm_provider.call_llm (the symbol wiki_engine imports lazily) so
    no real provider / network is touched — same isolation goal as the
    _PROVIDERS monkeypatch pattern in test_rule_miner.py.
    """
    calls: list[tuple[str, str]] = []

    def _fake(prompt: str, text: str = "", *args, **kwargs) -> str:
        calls.append((prompt, text))
        # Cleanup prompt expects JSON; breakdown expects a JSON array.
        if "rate it 1-10" in prompt:
            return json.dumps({"score": 3, "issues": ["dump"], "rewrite": "REWRITTEN BODY"})
        if "deserve their own article" in prompt:
            return json.dumps(
                [{"entity": "Widget", "description": "covers widgets", "mentioned_in": []}]
            )
        # absorb create / update paths want a long compiled-truth body.
        return (
            "## Summary\nThis is a stub compiled truth section that is well over "
            "fifty characters so the length guard passes cleanly."
        )

    import memorymaster.llm_provider as llm_provider

    monkeypatch.setattr(llm_provider, "call_llm", _fake, raising=False)
    return calls


def test_absorb_create_writes_article_with_llm_body(tmp_path, stub_llm):
    """A subject with >=2 claims and a non-empty LLM body produces one article.

    WHY: the create branch is the primary wiki-absorb path; if the >50-char
    guard or the write call regresses, no knowledge ever reaches the vault.
    """
    db = _fresh_db(tmp_path / "m.db")
    _seed(
        db,
        [
            {"subject": "Storage", "text": "Storage uses SQLite with WAL mode enabled."},
            {"subject": "Storage", "text": "Storage exposes FTS5 full text search."},
        ],
    )
    wiki = tmp_path / "wiki"

    result = wiki_engine.absorb(db, wiki, scope_filter="project:demo")

    assert result["articles_written"] == 1
    assert result["articles_updated"] == 0
    article = wiki / "project-demo" / "storage.md"
    assert article.exists()
    body = article.read_text(encoding="utf-8")
    assert "stub compiled truth" in body
    # Frontmatter schema invariants the validate-wiki hook enforces.
    assert "title: Storage" in body
    assert "explored: false" in body
    assert "type: fact" in body


def test_absorb_update_preserves_timeline(tmp_path, stub_llm):
    """Re-absorbing an existing subject rewrites truth but keeps the timeline.

    WHY: the timeline section is append-only evidence. The update branch must
    splice the new compiled truth ABOVE an existing '### YYYY-MM-DD' timeline
    block, never drop it. This anchors that contract.
    """
    db = _fresh_db(tmp_path / "m.db")
    _seed(
        db,
        [
            {"subject": "Cache", "text": "Cache uses a TTL of sixty seconds by default."},
            {"subject": "Cache", "text": "Cache invalidates on write to avoid stale reads."},
        ],
    )
    wiki = tmp_path / "wiki"
    wiki_engine.absorb(db, wiki, scope_filter="project:demo")
    article = wiki / "project-demo" / "cache.md"
    assert article.exists()
    # Inject a distinctive timeline marker the update path must preserve.
    original = article.read_text(encoding="utf-8")
    marker = "### 2026-01-01 | fact\nORIGINAL-TIMELINE-MARKER"
    article.write_text(original + "\n\n---\n\n## Timeline\n" + marker + "\n", encoding="utf-8")

    result = wiki_engine.absorb(db, wiki, scope_filter="project:demo")

    assert result["articles_updated"] == 1
    updated = article.read_text(encoding="utf-8")
    assert "ORIGINAL-TIMELINE-MARKER" in updated  # timeline survived
    assert "stub compiled truth" in updated  # truth rewritten


def test_absorb_empty_llm_writes_nothing(tmp_path, monkeypatch):
    """When the provider yields no text, no article is written.

    WHY: provider outage must NOT produce blank wiki pages. The <=50-char
    guard is the safety contract for graceful degradation.
    """
    db = _fresh_db(tmp_path / "m.db")
    _seed(
        db,
        [
            {"subject": "Auth", "text": "Auth validates tokens at the boundary."},
            {"subject": "Auth", "text": "Auth rejects expired sessions."},
        ],
    )
    monkeypatch.setattr(wiki_engine, "_call_llm", lambda *a, **k: "")
    wiki = tmp_path / "wiki"

    result = wiki_engine.absorb(db, wiki, scope_filter="project:demo")

    assert result["articles_written"] == 0
    assert not (wiki / "project-demo" / "auth.md").exists()


def test_absorb_skips_single_claim_subjects(tmp_path, stub_llm):
    """Subjects with fewer than two claims are skipped by absorb.

    WHY: a lone claim is not enough signal for a coherent article; the <2
    guard prevents thin pages.
    """
    db = _fresh_db(tmp_path / "m.db")
    _seed(db, [{"subject": "Lonely", "text": "Only one claim about this subject."}])
    wiki = tmp_path / "wiki"

    result = wiki_engine.absorb(db, wiki, scope_filter="project:demo")

    assert result["articles_written"] == 0
    assert not (wiki / "project-demo" / "lonely.md").exists()


def test_absorb_no_claims_returns_zero(tmp_path):
    """An empty DB returns a zeroed summary without touching the LLM."""
    db = _fresh_db(tmp_path / "m.db")
    result = wiki_engine.absorb(db, tmp_path / "wiki", scope_filter="project:demo")
    assert result["articles_written"] == 0
    assert result["articles_updated"] == 0


def test_absorb_single_claim_create_then_update(tmp_path, stub_llm):
    """absorb_single_claim creates the subject article, then updates it.

    WHY: this is the steward's per-claim immediate-absorb path; both the
    create and the was_update branches must round-trip a real DB row.
    """
    db = _fresh_db(tmp_path / "m.db")
    _seed(
        db,
        [
            {"subject": "Indexer", "text": "Indexer batches writes for throughput."},
            {"subject": "Indexer", "text": "Indexer flushes on shutdown to avoid loss."},
        ],
    )
    wiki = tmp_path / "wiki"

    first = wiki_engine.absorb_single_claim(1, db_path=db, wiki_dir=wiki)
    assert first["absorbed"] is True
    assert first["updated"] is False
    assert Path(first["article"]).exists()

    second = wiki_engine.absorb_single_claim(2, db_path=db, wiki_dir=wiki)
    assert second["absorbed"] is True
    assert second["updated"] is True


def test_absorb_single_claim_missing_returns_reason(tmp_path):
    """A non-existent / inactive claim id reports absorbed=False with a reason."""
    db = _fresh_db(tmp_path / "m.db")
    result = wiki_engine.absorb_single_claim(999, db_path=db, wiki_dir=tmp_path / "wiki")
    assert result["absorbed"] is False
    assert result["reason"] == "not_found_or_inactive"


def test_cleanup_rewrites_low_score_keeps_frontmatter(tmp_path, stub_llm):
    """cleanup rewrites a low-scoring article body but preserves frontmatter.

    WHY: cleanup must never strip the schema frontmatter (title/scope/tags);
    only the body below the second '---' is eligible for rewrite.
    """
    wiki = tmp_path / "wiki" / "project-demo"
    wiki.mkdir(parents=True)
    # Audited counter rewrites every 5th article; create 5 so #5 is audited.
    fm = "---\ntitle: A{n}\ntype: fact\nscope: project:demo\n---\n\n"
    body = "Original body paragraph that is comfortably longer than one hundred characters so it passes the length gate.\n"
    for n in range(1, 6):
        (wiki / f"a{n}.md").write_text(fm.format(n=n) + body, encoding="utf-8")

    result = wiki_engine.cleanup(tmp_path / "wiki")

    assert result["audited"] == 5
    assert result["rewritten"] == 1
    fifth = (wiki / "a5.md").read_text(encoding="utf-8")
    assert "REWRITTEN BODY" in fifth
    assert "title: A5" in fifth  # frontmatter preserved


def test_cleanup_ignores_short_and_underscore_files(tmp_path, stub_llm):
    """Index files and tiny stubs are not audited."""
    wiki = tmp_path / "wiki" / "project-demo"
    wiki.mkdir(parents=True)
    (wiki / "_index.md").write_text("# index\n" + "x" * 500, encoding="utf-8")
    (wiki / "tiny.md").write_text("too short", encoding="utf-8")

    result = wiki_engine.cleanup(tmp_path / "wiki")

    assert result["audited"] == 0
    assert result["rewritten"] == 0


def test_breakdown_finds_missing_and_delegates_to_absorb(tmp_path, stub_llm):
    """breakdown surfaces a subject with >=3 claims lacking an article.

    WHY: breakdown is how the wiki grows coverage for entities that only ever
    appear inside other articles; the >=3 threshold and the absorb delegation
    are the load-bearing parts.
    """
    db = _fresh_db(tmp_path / "m.db")
    _seed(
        db,
        [
            {"subject": "Widget", "text": "Widget renders a control."},
            {"subject": "Widget", "text": "Widget emits change events."},
            {"subject": "Widget", "text": "Widget supports theming."},
        ],
    )
    wiki = tmp_path / "wiki"
    wiki.mkdir()

    result = wiki_engine.breakdown(db, wiki, scope_filter="project:demo")

    assert result["missing"] == 1
    assert result["created"] >= 1


def test_breakdown_no_missing_returns_zero(tmp_path, stub_llm):
    """When every >=3-claim subject already has an article, nothing is created."""
    db = _fresh_db(tmp_path / "m.db")
    _seed(
        db,
        [
            {"subject": "Existing", "text": "Existing one."},
            {"subject": "Existing", "text": "Existing two."},
            {"subject": "Existing", "text": "Existing three."},
        ],
    )
    wiki = tmp_path / "wiki" / "project-demo"
    wiki.mkdir(parents=True)
    (wiki / "existing.md").write_text("# Existing\nbody", encoding="utf-8")

    result = wiki_engine.breakdown(db, tmp_path / "wiki", scope_filter="project:demo")

    assert result["missing"] == 0
    assert result["created"] == 0


# ---- frontmatter / helper branches -------------------------------------


def test_read_existing_explored_preserved(tmp_path):
    """explored: true is parsed back out so re-absorb preserves human review."""
    f = tmp_path / "art.md"
    f.write_text("---\ntitle: X\nexplored: true\n---\n\nbody\n", encoding="utf-8")
    assert wiki_engine._read_existing_explored(f) is True
    f.write_text("---\ntitle: X\nexplored: no\n---\n\nbody\n", encoding="utf-8")
    assert wiki_engine._read_existing_explored(f) is False


def test_read_existing_explored_missing_cases(tmp_path):
    """No file, no frontmatter, or no explored field all return None."""
    missing = tmp_path / "nope.md"
    assert wiki_engine._read_existing_explored(missing) is None
    no_fm = tmp_path / "plain.md"
    no_fm.write_text("just a body, no frontmatter\n", encoding="utf-8")
    assert wiki_engine._read_existing_explored(no_fm) is None
    no_field = tmp_path / "nofield.md"
    no_field.write_text("---\ntitle: X\n---\n\nbody\n", encoding="utf-8")
    assert wiki_engine._read_existing_explored(no_field) is None


def test_extract_description_skips_markup_and_truncates():
    """Description extraction skips headers/lists and cuts at a sentence."""
    body = (
        "# Heading\n\n- a list item\n\n"
        "This is the first real paragraph with enough substance. "
        "It then continues with a second sentence that pushes it well past the "
        "configured maximum character budget so truncation must kick in here."
    )
    desc = wiki_engine._extract_description(body, max_chars=80)
    assert desc
    assert "#" not in desc
    assert len(desc) <= 84  # allows the trailing "..." case
    assert wiki_engine._extract_description("") == ""


def test_build_tags_dedupes_and_caps():
    tags = wiki_engine._build_tags("decision", "project:demo", ["decision", "fact", "gotcha"])
    assert tags[0] == "decision"
    assert "project-demo" in tags
    assert tags.count("decision") == 1  # majority type not duplicated
    assert len(tags) <= 8


def test_yaml_escape_quotes_special_chars():
    assert wiki_engine._yaml_escape("plain") == "plain"
    assert wiki_engine._yaml_escape('has: colon') == '"has: colon"'
    assert wiki_engine._yaml_escape("") == '""'
    assert wiki_engine._yaml_escape(None) == '""'


def test_safe_name_and_scope_dirname():
    assert wiki_engine._safe_name("Hello World!") == "hello-world"
    assert wiki_engine._safe_name("") == "misc"
    assert wiki_engine._scope_dirname("project:demo") == "project-demo"
    assert wiki_engine._scope_dirname("global") == "global"
