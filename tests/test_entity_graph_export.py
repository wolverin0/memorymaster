from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
import xml.etree.ElementTree as ET

from memorymaster.surfaces.cli import main
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs.entity_graph_export import export_entity_graph


def _init_db(path) -> None:
    MemoryService(path, workspace_root=path.parent).init_db()


def _seed_five_entity_graph(path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    entities = [
        (1, "Alice", "person"),
        (2, "Bob", "person"),
        (3, "Acme", "org"),
        (4, "Qdrant", "product"),
        (5, "MemoryMaster", "project"),
    ]
    edges = [
        (1, 3, "works_at", 1.0, 1),
        (2, 3, "works_at", 1.0, 1),
        (5, 4, "uses", 2.5, 2),
        (5, 3, "depends_on", 1.2, 2),
    ]
    with sqlite3.connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO claims (id, text, scope, status, created_at, updated_at)
            VALUES (?, ?, ?, 'confirmed', ?, ?)
            """,
            [
                (1, "people at acme", "project:foo", now, now),
                (2, "memorymaster uses qdrant", "project:foo", now, now),
                (3, "other scope", "project:bar", now, now),
            ],
        )
        conn.executemany(
            "INSERT INTO entities "
            "(id, canonical_name, entity_type, scope, created_at, updated_at) "
            "VALUES (?, ?, ?, 'global', ?, ?)",
            [(entity_id, name, entity_type, now, now) for entity_id, name, entity_type in entities],
        )
        conn.execute(
            "INSERT INTO entity_aliases "
            "(entity_id, alias, variant_key, original_form, created_at) "
            "VALUES (1, 'a-example', 'A. Example', 'A. Example', ?)",
            (now,),
        )
        conn.executemany(
            """
            INSERT INTO entity_edges (source_id, target_id, relation, weight, claim_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [(source, target, relation, weight, claim_id, now) for source, target, relation, weight, claim_id in edges],
        )
        conn.executemany(
            "INSERT INTO claim_entity_links (claim_id, entity_id) VALUES (?, ?)",
            [(1, 1), (1, 2), (1, 3), (2, 5), (2, 4)],
        )


def test_export_empty_graph_dot(tmp_path):
    db_path = tmp_path / "empty.db"
    output = tmp_path / "graph.dot"
    _init_db(db_path)

    result = export_entity_graph(db_path, output, "dot")

    assert result.nodes == 0
    assert result.edges == 0
    assert output.read_text(encoding="utf-8") == (
        'digraph entity_graph {\n  graph [rankdir="LR"];\n  node [shape="ellipse"];\n}\n'
    )


def test_export_empty_graph_graphml(tmp_path):
    db_path = tmp_path / "empty.db"
    output = tmp_path / "graph.graphml"
    _init_db(db_path)

    result = export_entity_graph(db_path, output, "graphml")

    assert result.nodes == 0
    assert result.edges == 0
    root = ET.parse(output).getroot()
    graph = root.find("{http://graphml.graphdrawing.org/xmlns}graph")
    assert graph is not None
    assert graph.findall("{http://graphml.graphdrawing.org/xmlns}node") == []


def test_export_five_entity_graph_dot_with_scope(tmp_path):
    db_path = tmp_path / "graph.db"
    output = tmp_path / "graph.dot"
    _init_db(db_path)
    _seed_five_entity_graph(db_path)

    result = export_entity_graph(db_path, output, "dot", scope="project:foo")
    text = output.read_text(encoding="utf-8")

    assert result.nodes == 5
    assert result.edges == 4
    assert '"5" -> "4"' in text
    assert 'label="uses"' in text
    assert 'aliases="A. Example"' in text


def test_export_five_entity_graph_graphml_with_scope(tmp_path):
    db_path = tmp_path / "graph.db"
    output = tmp_path / "graph.graphml"
    _init_db(db_path)
    _seed_five_entity_graph(db_path)

    result = export_entity_graph(db_path, output, "graphml", scope="project:foo")
    root = ET.parse(output).getroot()
    ns = {"g": "http://graphml.graphdrawing.org/xmlns"}

    assert result.nodes == 5
    assert result.edges == 4
    assert len(root.findall(".//g:node", ns)) == 5
    assert len(root.findall(".//g:edge", ns)) == 4
    assert root.find(".//g:edge[@source='5'][@target='4']", ns) is not None


def test_entity_graph_export_cli_writes_dot(tmp_path):
    db_path = tmp_path / "graph.db"
    output = tmp_path / "graph.dot"
    _init_db(db_path)
    _seed_five_entity_graph(db_path)

    code = main([
        "--db", str(db_path),
        "entity-graph-export",
        "--format", "dot",
        "--output", str(output),
        "--scope", "project:foo",
    ])

    assert code == 0
    assert output.exists()
