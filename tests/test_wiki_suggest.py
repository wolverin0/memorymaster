from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from memorymaster.surfaces.cli import main
from memorymaster.entity_graph import EntityGraph
from memorymaster.wiki_suggest import suggest_wikilinks


def _seed_graph(db_path: Path) -> None:
    graph = EntityGraph(str(db_path))
    graph.ensure_tables()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                wiki_article TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO entities (id, name, type, aliases, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                ("e-claim", "claim", "concept", '["claims"]', "2026-01-01T00:00:00Z"),
                ("e-status", "status", "concept", "[]", "2026-01-01T00:00:00Z"),
                ("e-qdrant", "qdrant", "product", "[]", "2026-01-01T00:00:00Z"),
            ],
        )
        conn.executemany(
            "INSERT INTO entity_edges (source_id, target_id, relation, claim_id, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                ("e-claim", "e-status", "related_to", 1, "2026-01-01T00:00:00Z"),
                ("e-status", "e-qdrant", "related_to", 2, "2026-01-01T00:00:00Z"),
            ],
        )
        conn.executemany(
            "INSERT INTO claims (id, status, wiki_article) VALUES (?, ?, ?)",
            [
                (1, "confirmed", "claims-lifecycle"),
                (2, "confirmed", "status-governance"),
                (3, "confirmed", "qdrant-sync"),
                (4, "confirmed", "missing-article"),
            ],
        )
        conn.executemany(
            "INSERT INTO claim_entity_links (claim_id, entity_id) VALUES (?, ?)",
            [
                (1, "e-claim"),
                (1, "e-status"),
                (2, "e-status"),
                (3, "e-qdrant"),
                (4, "e-claim"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _seed_wiki(root: Path) -> None:
    root.mkdir(parents=True)
    for slug in ("claims-lifecycle", "status-governance", "qdrant-sync"):
        (root / f"{slug}.md").write_text(f"# {slug}\n", encoding="utf-8")


def test_suggest_wikilinks_ranks_by_graph_proximity(tmp_path: Path) -> None:
    db_path = tmp_path / "memorymaster.db"
    wiki_root = tmp_path / "wiki"
    _seed_graph(db_path)
    _seed_wiki(wiki_root)

    suggestions = suggest_wikilinks(
        db_path,
        "The claims lifecycle should account for status changes.",
        wiki_root=wiki_root,
        limit=5,
        hops=2,
    )

    assert suggestions[0] == {
        "slug": "claims-lifecycle",
        "score": 1.0,
        "matched_entities": ["claim", "status"],
    }
    assert [item["slug"] for item in suggestions] == [
        "claims-lifecycle",
        "status-governance",
        "qdrant-sync",
    ]
    assert suggestions[2]["score"] < suggestions[0]["score"]


def test_wiki_suggest_links_cli_prints_json_list(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "memorymaster.db"
    wiki_root = tmp_path / "wiki"
    _seed_graph(db_path)
    _seed_wiki(wiki_root)

    rc = main([
        "--db",
        str(db_path),
        "wiki-suggest-links",
        "--text",
        "claim status",
        "--wiki-root",
        str(wiki_root),
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["slug"] == "claims-lifecycle"


def test_suggest_wikilinks_returns_empty_for_missing_graph(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    _seed_wiki(wiki_root)

    assert suggest_wikilinks(tmp_path / "empty.db", "claim status", wiki_root=wiki_root) == []
