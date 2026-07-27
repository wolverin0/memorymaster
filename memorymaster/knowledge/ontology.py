"""Versioned personal ontology registry and startup validation."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ONTOLOGY_VERSION = "personal-v1"
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_VERSION = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")

PERSONAL_ENTITY_TYPES = frozenset(
    {
        "person",
        "organization",
        "place",
        "product",
        "concept",
        "project",
        "system",
        "service",
        "device",
        "document",
        "event",
        "decision",
        "commitment",
    }
)

_RELATION_ROWS = (
    ("works_at", True, False),
    ("located_in", True, False),
    ("owns", True, False),
    ("part_of", True, False),
    ("related_to", False, True),
    ("manages", True, False),
    ("created_by", True, False),
    ("uses", True, False),
    ("depends_on", True, False),
    ("participates_in", True, False),
    ("decided", True, False),
    ("committed_to", True, False),
    ("supersedes", True, False),
)


@dataclass(frozen=True, slots=True)
class RelationDefinition:
    name: str
    directed: bool
    symmetric: bool


@dataclass(frozen=True, slots=True)
class Ontology:
    version: str
    entity_types: frozenset[str]
    relations: dict[str, RelationDefinition]

    def prompt(self) -> str:
        entity_values = "|".join(sorted(self.entity_types))
        relation_values = "|".join(sorted(self.relations))
        return (
            f"Use ontology {self.version}. Return one JSON object only with "
            '{"entities":[{"name":"canonical name","type":"'
            + entity_values
            + '","aliases":["alt"]}],"relations":[{"source":"entity name",'
            + '"target":"entity name","relation":"'
            + relation_values
            + '"}]}. Only include explicitly supported values and evidence.'
        )


def _base_ontology() -> Ontology:
    relations = {
        name: RelationDefinition(name, directed, symmetric)
        for name, directed, symmetric in _RELATION_ROWS
    }
    return Ontology(ONTOLOGY_VERSION, PERSONAL_ENTITY_TYPES, relations)


def _names(value: Any, field: str) -> set[str]:
    if not isinstance(value, list):
        raise ValueError(f"custom ontology {field} must be a list")
    names = {str(item).strip().lower() for item in value}
    if any(not _NAME.fullmatch(name) for name in names):
        raise ValueError(f"custom ontology {field} contains an invalid name")
    return names


def _custom_relations(value: Any) -> dict[str, RelationDefinition]:
    if not isinstance(value, list):
        raise ValueError("custom ontology relations must be a list")
    relations: dict[str, RelationDefinition] = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("custom ontology relation rows must be objects")
        name = str(row.get("name", "")).strip().lower()
        if not _NAME.fullmatch(name):
            raise ValueError("custom ontology relation name is invalid")
        symmetric = bool(row.get("symmetric", False))
        directed = bool(row.get("directed", not symmetric))
        if symmetric and directed:
            raise ValueError(f"symmetric relation '{name}' cannot also be directed")
        relations[name] = RelationDefinition(name, directed, symmetric)
    return relations


def load_ontology(path: str | Path | None = None) -> Ontology:
    """Load personal-v1 plus an optional validated additive JSON file."""
    configured = str(path or os.environ.get("MEMORYMASTER_ONTOLOGY_FILE", "")).strip()
    base = _base_ontology()
    if not configured:
        return base
    payload = json.loads(Path(configured).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("custom ontology must be a JSON object")
    version = str(payload.get("version", "")).strip()
    if not version or not _VERSION.fullmatch(version):
        raise ValueError("custom ontology version is invalid")
    entity_types = base.entity_types | _names(payload.get("entity_types", []), "entity_types")
    relations = {**base.relations, **_custom_relations(payload.get("relations", []))}
    return Ontology(f"{base.version}+{version}", frozenset(entity_types), relations)
