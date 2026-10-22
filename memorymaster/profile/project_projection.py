"""Deterministic, explicitly authorized project projections and validated views."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Iterator

from memorymaster.core.generated_artifacts import (
    GENERATED_OPENER as OPENER,
    GENERATED_CLOSER as CLOSER,
)
from memorymaster.profile.project_repository import (
    digest,
    load_project_snapshot,
    utc_now,
)

SCHEMA = "memorymaster.project-projection.v1"
SECTIONS = ("Purpose", "Constraints", "Decisions", "Open cards", "Gotchas")
MAX_MANIFEST_BYTES = 256 * 1024


@dataclass(frozen=True)
class ProjectProjectionConfig:
    db_path: Path
    scope: str
    tenant_id: str | None
    context_key: str
    epoch: str
    project_root: Path
    output_root: Path
    state_root: Path
    ledger_root: Path | None = None
    ledger_repo: str | None = None
    ledger_filtered: bool = False
    aliases: tuple[str, ...] = ()
    enabled: bool = False
    budget_bytes: int = 2200

    def __post_init__(self) -> None:
        if str(self.db_path).startswith(("postgres:", "postgresql:")):
            raise ValueError("SQLite required")
        for name in ("context_key", "epoch"):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", getattr(self, name)):
                raise ValueError("invalid context identity")
        if not re.fullmatch(r"project:[a-z0-9][a-z0-9_.-]{0,95}", self.scope):
            raise ValueError("invalid project scope")
        if (
            any(not re.fullmatch(r"project:[a-z0-9][a-z0-9_.-]{0,95}", alias) for alias in self.aliases)
            or len(self.aliases) > 8
        ):
            raise ValueError("invalid aliases")
        if not 400 <= self.budget_bytes <= 2200:
            raise ValueError("project cap must be 400..2200 UTF-8 bytes")
        for name in ("db_path", "project_root", "output_root", "state_root", "ledger_root"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value).resolve())
        object.__setattr__(self, "aliases", tuple(self.aliases))

    @property
    def slug(self) -> str:
        return self.scope.split(":", 1)[1]


def projection_paths(config: ProjectProjectionConfig) -> tuple[Path, Path]:
    root = config.output_root / config.context_key
    return root / f"project-{config.slug}.md", root / f"project-{config.slug}.json"


@contextmanager
def project_lock(path: Path, timeout: float = 0.05) -> Iterator[bool]:
    """Nonblocking kernel lock; the OS releases ownership after process death."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    acquired = False
    try:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + max(0, timeout)
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.005)
        yield acquired
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: dict) -> None:
    _atomic_write(path, json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())


@dataclass(frozen=True)
class ProjectionItem:
    key: str
    section: str
    text: str
    support: dict
    sort: list = field(default_factory=list)


@dataclass
class ProjectProjection:
    payload: str
    manifest: dict
    items: list[dict]
    status: str

    @property
    def markdown(self) -> str:
        return f"{OPENER}\n{self.payload}\n{CLOSER}\n"

    @property
    def public_manifest(self) -> dict:
        return {
            key: value
            for key, value in self.manifest.items()
            if key not in ("source_coverage", "source_fingerprints", "tenant_id", "aliases", "context_key")
        }


def _line(item: dict) -> str:
    support = item["support"]
    reference = f"claim:{support['id']}" if support["kind"] == "claim" else str(support["id"])
    return f"- {item['text']} [{reference}]"


def render_project_projection(
    config: ProjectProjectionConfig,
    items: list[dict],
    *,
    generation: str,
    read_at: str,
    coverage: dict | None = None,
    diagnostics: list[str] | None = None,
    omissions: dict | None = None,
    fingerprints: dict | None = None,
    source_complete: bool = True,
    next_temporal_boundary: str | None = None,
) -> ProjectProjection:
    selected = [dict(item) for item in items]
    diagnostics = list(diagnostics or [])
    omissions = dict(omissions or {})
    decisions = [item for item in selected if item["section"] == "Decisions"]
    mandatory_decisions = {item["key"] for item in decisions[:3]}
    if len(decisions) < 3:
        diagnostics.append("m2_not_ready:decisions_incomplete")
    # Purpose and pinned safety prose share a strict byte allocation. Shortening
    # literal excerpts keeps references and support identities intact.
    safety = [item for item in selected if item["section"] == "Purpose" or item.get("pinned")]
    remaining = 400
    for item in safety:
        cap = min(remaining, 400 // max(1, len(safety)))
        if cap < 4:
            selected.remove(item)
            omissions["pinned_constraints"] = omissions.get("pinned_constraints", 0) + 1
            diagnostics.append("m2_not_ready:pinned_constraints_trimmed")
            continue
        text = item["text"]
        while len(text.encode()) > cap:
            text = text[:-1]
        if text != item["text"]:
            while text and len((text + "…").encode()) > cap:
                text = text[:-1]
            text += "…"
        item["text"] = text
        remaining -= len(item["text"].encode())
    diagnostics = list(dict.fromkeys(diagnostics))
    max_bytes = config.budget_bytes
    status = "partial" if diagnostics or any(omissions.values()) else "ready"

    def compose() -> tuple[str, str]:
        nonlocal status
        status = "partial" if diagnostics or any(omissions.values()) else "ready"
        identity = digest(
            [
                generation,
                [(item["key"], item["support"], item["text"]) for item in selected],
                status,
                omissions,
                diagnostics,
            ]
        )
        lines = [f"Project {config.slug}", f"View {identity[:16]} | {status}"]
        for section in SECTIONS:
            lines.append(f"## {section}")
            matches = [item for item in selected if item["section"] == section]
            if section == "Purpose" and not matches:
                lines.append("Purpose not recorded in confirmed memory.")
            lines.extend(_line(item) for item in matches)
        if diagnostics:
            lines.append("Status: " + ", ".join(dict.fromkeys(diagnostics)))
        if any(omissions.values()):
            lines.append("Omitted: " + ", ".join(f"{key}={value}" for key, value in sorted(omissions.items()) if value))
        return "\n".join(lines), identity

    def size(payload: str) -> int:
        return len(f"{OPENER}\n{payload}\n{CLOSER}\n".encode())

    # Reserve the complete compact card set before optional prose; exact support
    # records are generated only after the final trim.
    compacted = False
    while True:
        payload, identity = compose()
        if (
            size(payload) <= max_bytes
            and len(selected) <= 64
            and sum(len(item["support"].get("citations", [])) for item in selected) <= 256
        ):
            break
        if not compacted:
            for item in selected:
                text = item["text"]
                minimum = 48 if item["section"] == "Open cards" else 80
                if len(text) > minimum:
                    item["text"] = text[: minimum - 1] + "…"
            compacted = True
            continue
        optional = (
            lambda item: item["section"] == "Gotchas",
            lambda item: item["section"] == "Decisions" and item["key"] not in mandatory_decisions,
            lambda item: item["section"] == "Constraints" and not item.get("pinned"),
            lambda item: item["section"] == "Purpose",
            lambda item: item["section"] == "Constraints",
            lambda item: item["section"] == "Decisions",
            lambda item: item["section"] == "Open cards",
        )
        removable = next((item for predicate in optional for item in reversed(selected) if predicate(item)), None)
        if removable is None:
            raise ValueError("mandatory_projection_framing_does_not_fit")
        selected.remove(removable)
        key = removable["section"].lower().replace(" ", "_")
        omissions[key] = omissions.get(key, 0) + 1
        if removable["section"] == "Open cards" and "m2_not_ready:open_cards_trimmed" not in diagnostics:
            diagnostics.append("m2_not_ready:open_cards_trimmed")
        if removable["section"] == "Decisions" and "m2_not_ready:decisions_trimmed" not in diagnostics:
            if removable["key"] in mandatory_decisions:
                diagnostics.append("m2_not_ready:decisions_trimmed")
        if removable.get("pinned") and "m2_not_ready:pinned_constraints_trimmed" not in diagnostics:
            diagnostics.append("m2_not_ready:pinned_constraints_trimmed")
    records = []
    lines = payload.splitlines()
    for item in selected:
        rendered = _line(item)
        index = lines.index(rendered)
        start = len(("\n".join(lines[:index]) + "\n").encode()) if index else 0
        records.append(
            {
                "key": item["key"],
                "section": item["section"],
                "text": item["text"],
                "line": index + 1,
                "span": [start, start + len(rendered.encode())],
                "text_hash": digest(item["text"]),
                "supports": [item["support"]],
                "pinned": item.get("pinned", False),
            }
        )
    markdown = f"{OPENER}\n{payload}\n{CLOSER}\n"
    manifest = {
        "schema": SCHEMA,
        "algorithm_version": 1,
        "scope": config.scope,
        "aliases": list(config.aliases),
        "tenant_id": config.tenant_id,
        "context_key": config.context_key,
        "activation_epoch": config.epoch,
        "generation_id": generation,
        "view_id": identity,
        "read_at": read_at,
        "checked_at": utc_now(),
        "output_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
        "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "output_bytes": len(markdown.encode()),
        "payload_bytes": len(payload.encode()),
        "budget_bytes": max_bytes,
        "items": records,
        "source_coverage": coverage or {},
        "source_fingerprints": fingerprints or {},
        "omissions": omissions,
        "diagnostics": list(dict.fromkeys(diagnostics)),
        "status": status,
        "complete": source_complete,
        "source_complete": source_complete,
        "next_temporal_boundary": next_temporal_boundary,
    }
    return ProjectProjection(payload, manifest, selected, status)


def build_project_projection(config: ProjectProjectionConfig) -> ProjectProjection:
    if not config.enabled:
        raise ValueError("projection inactive")
    snapshot = load_project_snapshot(config)
    ordered = []
    for section in SECTIONS:
        matches = [item for item in snapshot["items"].values() if item["section"] == section and item.get("open", True)]
        matches.sort(key=lambda item: item["sort"], reverse=section == "Decisions")
        ordered.extend(matches[:1] if section == "Purpose" else matches[:10] if section == "Decisions" else matches)
    diagnostics = list(snapshot["diagnostics"])
    if not snapshot["complete"] and not diagnostics:
        diagnostics.append("source_incomplete")
    if any(diagnostic.startswith("ledger_") for diagnostic in diagnostics):
        diagnostics.append("m2_not_ready:ledger_incomplete")
    generation = digest([config.scope, config.context_key, config.epoch, snapshot["read_at"], snapshot["coverage"]])
    return render_project_projection(
        config,
        ordered,
        generation=generation,
        read_at=snapshot["read_at"],
        coverage=snapshot["coverage"],
        diagnostics=diagnostics,
        fingerprints=snapshot["fingerprints"],
        source_complete=snapshot["complete"],
        next_temporal_boundary=snapshot["next_temporal_boundary"],
    )


def publish_project_projection(config: ProjectProjectionConfig, projection: ProjectProjection) -> bool:
    if not config.enabled:
        return False
    md, path = projection_paths(config)
    with project_lock(path.with_suffix(".lock"), timeout=0) as acquired:
        if not acquired:
            return False
        if len(json.dumps(projection.manifest).encode()) > MAX_MANIFEST_BYTES:
            return False
        _atomic_write(md, projection.markdown.encode())
        atomic_json(path, projection.manifest)
        return True


def read_manifest(config: ProjectProjectionConfig) -> dict | None:
    if not config.enabled:
        return None
    md, path = projection_paths(config)
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES or md.stat().st_size > config.budget_bytes:
            return None
        with path.open("rb") as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        with md.open("rb") as stream:
            output = stream.read(config.budget_bytes + 1)
        manifest = json.loads(raw)
        if (
            manifest["schema"] != SCHEMA
            or manifest["scope"] != config.scope
            or manifest["tenant_id"] != config.tenant_id
            or manifest["context_key"] != config.context_key
            or manifest["activation_epoch"] != config.epoch
            or manifest["aliases"] != list(config.aliases)
            or manifest["budget_bytes"] != config.budget_bytes
            or hashlib.sha256(output).hexdigest() != manifest["output_sha256"]
            or len(output) != manifest["output_bytes"]
            or len(output) > config.budget_bytes
        ):
            return None
        text = output.decode()
        if not text.startswith(OPENER + "\n") or not text.endswith("\n" + CLOSER + "\n"):
            return None
        payload = text[len(OPENER) + 1 : -len(CLOSER) - 2]
        if (
            hashlib.sha256(payload.encode()).hexdigest() != manifest["payload_sha256"]
            or len(payload.encode()) != manifest["payload_bytes"]
        ):
            return None
        records = manifest["items"]
        if (
            len(records) > 64
            or len(json.dumps(manifest["source_coverage"]).encode()) > 128 * 1024
            or len(manifest["source_coverage"]) > 769
        ):
            return None
        citations = 0
        keys = set()
        for record in records:
            if record["key"] in keys or len(record["supports"]) != 1 or record["section"] not in SECTIONS:
                return None
            keys.add(record["key"])
            support = record["supports"][0]
            if support["kind"] == "claim":
                if type(support["id"]) is not int or support["id"] <= 0:
                    return None
                citations += len(support["citations"])
            elif support["kind"] != "card" or not isinstance(support["id"], str):
                return None
            if record["text_hash"] != digest(record["text"]):
                return None
            rendered = _line({"text": record["text"], "support": support})
            line_index = record["line"] - 1
            lines = payload.splitlines()
            if line_index < 0 or line_index >= len(lines) or lines[line_index] != rendered:
                return None
            start = len(("\n".join(lines[:line_index]) + "\n").encode()) if line_index else 0
            if record["span"] != [start, start + len(rendered.encode())]:
                return None
        if citations > 256:
            return None
        expected_generation = digest(
            [config.scope, config.context_key, config.epoch, manifest["read_at"], manifest["source_coverage"]]
        )
        expected_view = digest(
            [
                manifest["generation_id"],
                [(record["key"], record["supports"][0], record["text"]) for record in records],
                manifest["status"],
                manifest["omissions"],
                manifest["diagnostics"],
            ]
        )
        if (
            manifest["generation_id"] != expected_generation
            or manifest["view_id"] != expected_view
            or f"View {expected_view[:16]} | {manifest['status']}" not in payload.splitlines()
        ):
            return None
        return manifest
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return None


def read_project_projection(config: ProjectProjectionConfig) -> ProjectProjection | None:
    manifest = read_manifest(config)
    if manifest is None:
        return None
    try:
        checked = datetime.fromisoformat(manifest["checked_at"])
        if checked.tzinfo is None or (datetime.now(timezone.utc) - checked).total_seconds() > 3600:
            return None
    except (ValueError, TypeError, KeyError):
        return None
    ids = [record["supports"][0]["id"] for record in manifest["items"] if record["supports"][0]["kind"] == "claim"]
    current = load_project_snapshot(config, claim_ids=ids, validation=True)
    selected = []
    diagnostics = list(manifest["diagnostics"])
    omissions = dict(manifest["omissions"])
    for record in manifest["items"]:
        item = current["items"].get(record["key"])
        if record["supports"][0]["kind"] == "card":
            # Modified still-open cards are safe to render from the authorized
            # ledger snapshot and retain their reserved place in the payload.
            if item and item.get("open"):
                if item["support"] == record["supports"][0]:
                    item = {**item, "text": record["text"]}
                selected.append(item)
            elif "m2_not_ready:cards_changed" not in diagnostics:
                diagnostics.append("m2_not_ready:cards_changed")
        elif item and item["support"] == record["supports"][0]:
            # Keep an unrelated bullet byte-identical even when the original
            # renderer shortened it to fit the shared allocation.
            selected.append({**item, "text": record["text"]})
        else:
            diagnostics.append("m2_not_ready:context_withdrawn")
    diagnostics.extend(current["diagnostics"])
    if any(key.startswith("card:") and key not in manifest["source_coverage"] for key in current["coverage"]):
        diagnostics.append("m2_not_ready:cards_changed")
    if not current["complete"] and "source_incomplete" not in diagnostics:
        diagnostics.append("source_incomplete")
    result = render_project_projection(
        config,
        selected,
        generation=manifest["generation_id"],
        read_at=manifest["read_at"],
        coverage=manifest["source_coverage"],
        diagnostics=list(dict.fromkeys(diagnostics)),
        omissions=omissions,
        fingerprints=manifest["source_fingerprints"],
        source_complete=current["complete"],
        next_temporal_boundary=manifest.get("next_temporal_boundary"),
    )
    result.manifest["checked_at"] = manifest["checked_at"]
    return result
