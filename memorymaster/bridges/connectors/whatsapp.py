"""WhatsApp import support for Atlas Inbox.

The importer accepts JSON/JSONL exports from wacli-like tools and normalizes
them into external_sources, source_items, and evidence_items. It is intentionally
tolerant of field names because WhatsApp export tools differ by version.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WhatsAppImportResult:
    source_id: int
    source_items_seen: int
    source_items_imported: int
    source_items_updated: int
    evidence_items_added: int
    duplicates_seen: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def import_wacli_json(
    service,
    input_path: str | Path,
    *,
    display_name: str = "WhatsApp",
    chat_id: str | None = None,
) -> WhatsAppImportResult:
    path = Path(input_path)
    rows = _load_message_rows(path)
    source = service.upsert_external_source(
        source_type="whatsapp",
        display_name=display_name,
        config_json={"importer": "wacli-json", "last_input_name": path.name},
    )

    seen_keys: set[str] = set()
    source_items_seen = 0
    source_items_imported = 0
    source_items_updated = 0
    evidence_items_added = 0
    duplicates_seen = 0

    for index, raw in enumerate(rows):
        normalized = _normalize_message(raw, index=index, fallback_chat_id=chat_id)
        source_item_id = normalized["source_item_id"]
        if source_item_id in seen_keys:
            duplicates_seen += 1
            continue
        seen_keys.add(source_item_id)
        source_items_seen += 1

        existing = service.get_source_item(source_id=source.id, source_item_id=source_item_id)
        item = service.upsert_source_item(
            source_id=source.id,
            source_item_id=source_item_id,
            item_type=normalized["item_type"],
            chat_id=normalized["chat_id"],
            sender_id=normalized["sender_id"],
            sender_name=normalized["sender_name"],
            occurred_at=normalized["occurred_at"],
            text=normalized["text"],
            payload_json=raw,
            content_hash=normalized["content_hash"],
        )
        if existing is None:
            source_items_imported += 1
        else:
            source_items_updated += 1

        if normalized["text"] and not _has_message_text_evidence(service, item.id):
            service.add_evidence_item(
                source_item_id=item.id,
                evidence_type="message_text",
                text=normalized["text"],
                provider="wacli",
                confidence=1.0,
                payload_json={"source_item_id": source_item_id},
            )
            evidence_items_added += 1

    return WhatsAppImportResult(
        source_id=source.id,
        source_items_seen=source_items_seen,
        source_items_imported=source_items_imported,
        source_items_updated=source_items_updated,
        evidence_items_added=evidence_items_added,
        duplicates_seen=duplicates_seen,
    )


def _load_message_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"WhatsApp import file not found: {path}")
    raw_text = path.read_text(encoding="utf-8-sig")
    stripped = raw_text.strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return [_as_dict(json.loads(line)) for line in raw_text.splitlines() if line.strip()]
    return _extract_messages(payload)


def _extract_messages(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [_as_dict(item) for item in payload]
    if not isinstance(payload, dict):
        return []

    for key in ("messages", "data", "items", "rows"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [_with_parent(item, payload) for item in rows]

    chats = payload.get("chats")
    if isinstance(chats, list):
        rows: list[dict[str, Any]] = []
        for chat in chats:
            if not isinstance(chat, dict):
                continue
            chat_messages = chat.get("messages")
            if not isinstance(chat_messages, list):
                continue
            for item in chat_messages:
                row = _with_parent(item, chat)
                rows.append(row)
        return rows

    return [_as_dict(payload)]


def _with_parent(item: Any, parent: dict[str, Any]) -> dict[str, Any]:
    row = _as_dict(item)
    for key in ("chat_id", "chatId", "chat_jid", "jid", "chat_name", "name"):
        if key in parent and key not in row:
            row[key] = parent[key]
    return row


def _as_dict(item: Any) -> dict[str, Any]:
    return item if isinstance(item, dict) else {"value": item}


def _normalize_message(raw: dict[str, Any], *, index: int, fallback_chat_id: str | None) -> dict[str, str | None]:
    chat = _first(raw, "chat_id", "chatId", "chat_jid", "chatJid", "remoteJid", "jid", "conversation_id")
    sender_id = _first(raw, "sender_id", "senderId", "from", "participant", "author", "user")
    sender_name = _first(raw, "sender_name", "senderName", "pushName", "notifyName", "name")
    occurred_at = _first(raw, "timestamp", "time", "date", "created_at", "createdAt", "messageTimestamp")
    text = _first(raw, "text", "body", "message", "caption", "content")
    source_item_id = _first(raw, "id", "message_id", "messageId", "key_id", "keyId", "stanzaId")
    item_type = _first(raw, "type", "message_type", "messageType", "media_type", "mediaType") or "message"

    normalized_chat = _clean(chat) or _clean(fallback_chat_id)
    normalized_text = _clean(text)
    normalized_source_item_id = _clean(source_item_id)
    if not normalized_source_item_id:
        normalized_source_item_id = _fallback_message_id(
            chat_id=normalized_chat,
            sender_id=_clean(sender_id),
            occurred_at=_clean(occurred_at),
            text=normalized_text,
            index=index,
        )
    content_hash = _content_hash(
        normalized_chat,
        _clean(sender_id),
        _clean(occurred_at),
        normalized_text,
    )
    return {
        "source_item_id": normalized_source_item_id,
        "item_type": _clean(item_type).lower() if _clean(item_type) else "message",
        "chat_id": normalized_chat,
        "sender_id": _clean(sender_id),
        "sender_name": _clean(sender_name),
        "occurred_at": _clean(occurred_at),
        "text": normalized_text,
        "content_hash": content_hash,
    }


def _first(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    key_payload = raw.get("key")
    if isinstance(key_payload, dict):
        for key in keys:
            value = key_payload.get(key)
            if value not in (None, ""):
                return value
    return None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _fallback_message_id(
    *,
    chat_id: str | None,
    sender_id: str | None,
    occurred_at: str | None,
    text: str | None,
    index: int,
) -> str:
    digest = _content_hash(chat_id, sender_id, occurred_at, text, str(index))
    return f"generated:{digest[:24]}"


def _content_hash(*parts: str | None) -> str:
    joined = "\n".join(part or "" for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _has_message_text_evidence(service, source_item_id: int) -> bool:
    evidence_items = service.list_evidence_items(
        source_item_id=source_item_id,
        evidence_type="message_text",
        limit=1,
    )
    return bool(evidence_items)
