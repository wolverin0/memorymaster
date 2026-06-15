"""Export the SQLite entity graph to interoperable graph formats."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import sqlite3
import xml.etree.ElementTree as ET

from memorymaster.knowledge.entity_graph import EntityGraph


SUPPORTED_FORMATS = {"dot", "graphml"}


@dataclass(frozen=True)
class EntityNode:
    id: str
    name: str
    type: str
    aliases: str


@dataclass(frozen=True)
class EntityEdge:
    source_id: str
    target_id: str
    relation: str
    weight: float
    claim_id: int | None


@dataclass(frozen=True)
class ExportResult:
    output: str
    format: str
    nodes: int
    edges: int
    scope: str | None


def export_entity_graph(db_path: str | Path, output: str | Path, fmt: str, scope: str | None = None) -> ExportResult:
    """Write entity graph data to DOT or GraphML."""
    normalized_format = fmt.lower()
    if normalized_format not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported format: {fmt}")

    graph = EntityGraph(str(db_path))
    graph.ensure_tables()
    conn = graph._connect()
    try:
        nodes, edges = _load_graph(conn, scope)
    finally:
        conn.close()

    rendered = _render_dot(nodes, edges) if normalized_format == "dot" else _render_graphml(nodes, edges)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    return ExportResult(str(output_path), normalized_format, len(nodes), len(edges), scope)


def _load_graph(conn: sqlite3.Connection, scope: str | None) -> tuple[list[EntityNode], list[EntityEdge]]:
    if not _has_entity_graph_schema(conn):
        return [], []
    if scope:
        edge_rows = conn.execute(
            """
            SELECT e.source_id, e.target_id, e.relation, e.weight, e.claim_id
            FROM entity_edges e
            JOIN claims c ON c.id = e.claim_id
            WHERE c.scope = ?
            ORDER BY e.source_id, e.target_id, e.relation
            """,
            (scope,),
        ).fetchall()
        node_rows = conn.execute(
            """
            SELECT DISTINCT en.id, en.name, en.type, en.aliases
            FROM entities en
            WHERE en.id IN (
                SELECT cel.entity_id
                FROM claim_entity_links cel
                JOIN claims c ON c.id = cel.claim_id
                WHERE c.scope = ?
                UNION
                SELECT e.source_id FROM entity_edges e JOIN claims c ON c.id = e.claim_id WHERE c.scope = ?
                UNION
                SELECT e.target_id FROM entity_edges e JOIN claims c ON c.id = e.claim_id WHERE c.scope = ?
            )
            ORDER BY en.name COLLATE NOCASE
            """,
            (scope, scope, scope),
        ).fetchall()
    else:
        edge_rows = conn.execute(
            """
            SELECT source_id, target_id, relation, weight, claim_id
            FROM entity_edges
            ORDER BY source_id, target_id, relation
            """
        ).fetchall()
        node_rows = conn.execute(
            "SELECT id, name, type, aliases FROM entities ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [_node_from_row(row) for row in node_rows], [_edge_from_row(row) for row in edge_rows]


def _has_entity_graph_schema(conn: sqlite3.Connection) -> bool:
    table_names = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('entities', 'entity_edges')"
        ).fetchall()
    }
    if table_names != {"entities", "entity_edges"}:
        return False
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(entities)").fetchall()}
    return {"id", "name", "type", "aliases"}.issubset(columns)


def _node_from_row(row: sqlite3.Row) -> EntityNode:
    return EntityNode(row["id"], row["name"], row["type"], _aliases_text(row["aliases"]))


def _edge_from_row(row: sqlite3.Row) -> EntityEdge:
    return EntityEdge(row["source_id"], row["target_id"], row["relation"], float(row["weight"]), row["claim_id"])


def _aliases_text(raw_aliases: str) -> str:
    try:
        aliases = json.loads(raw_aliases or "[]")
    except json.JSONDecodeError:
        return ""
    return ", ".join(str(alias) for alias in aliases)


def _render_dot(nodes: list[EntityNode], edges: list[EntityEdge]) -> str:
    lines = ["digraph entity_graph {", '  graph [rankdir="LR"];', '  node [shape="ellipse"];']
    for node in nodes:
        attrs = {"label": node.name, "type": node.type}
        if node.aliases:
            attrs["aliases"] = node.aliases
        lines.append(f"  {_dot_quote(node.id)} [{_dot_attrs(attrs)}];")
    for edge in edges:
        attrs = {"label": edge.relation, "relation": edge.relation, "weight": f"{edge.weight:g}"}
        if edge.claim_id is not None:
            attrs["claim_id"] = str(edge.claim_id)
        lines.append(f"  {_dot_quote(edge.source_id)} -> {_dot_quote(edge.target_id)} [{_dot_attrs(attrs)}];")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _dot_attrs(attrs: dict[str, str]) -> str:
    return ", ".join(f"{key}={_dot_quote(value)}" for key, value in attrs.items())


def _dot_quote(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{text}"'


def _render_graphml(nodes: list[EntityNode], edges: list[EntityEdge]) -> str:
    ET.register_namespace("", "http://graphml.graphdrawing.org/xmlns")
    root = ET.Element("{http://graphml.graphdrawing.org/xmlns}graphml")
    _add_graphml_keys(root)
    graph = ET.SubElement(root, "{http://graphml.graphdrawing.org/xmlns}graph", id="entity_graph", edgedefault="directed")
    for node in nodes:
        node_el = ET.SubElement(graph, "{http://graphml.graphdrawing.org/xmlns}node", id=node.id)
        _add_data(node_el, {"label": node.name, "type": node.type, "aliases": node.aliases})
    for index, edge in enumerate(edges):
        edge_el = ET.SubElement(
            graph,
            "{http://graphml.graphdrawing.org/xmlns}edge",
            id=f"e{index}",
            source=edge.source_id,
            target=edge.target_id,
        )
        _add_data(edge_el, {
            "relation": edge.relation,
            "label": edge.relation,
            "weight": f"{edge.weight:g}",
            "claim_id": "" if edge.claim_id is None else str(edge.claim_id),
        })
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def _add_graphml_keys(root: ET.Element) -> None:
    keys = [
        ("label", "all", "label", "string"),
        ("type", "node", "type", "string"),
        ("aliases", "node", "aliases", "string"),
        ("relation", "edge", "relation", "string"),
        ("weight", "edge", "weight", "double"),
        ("claim_id", "edge", "claim_id", "string"),
    ]
    for key_id, domain, name, attr_type in keys:
        ET.SubElement(root, "{http://graphml.graphdrawing.org/xmlns}key", id=key_id, **{
            "for": domain,
            "attr.name": name,
            "attr.type": attr_type,
        })


def _add_data(element: ET.Element, values: dict[str, str]) -> None:
    for key, value in values.items():
        data = ET.SubElement(element, "{http://graphml.graphdrawing.org/xmlns}data", key=key)
        data.text = value
