from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts import messages_to_turns
except ImportError:
    import messages_to_turns  # type: ignore[no-redef]


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = _to_str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Email connector config must be a JSON object")
    return payload


def _read_cursor(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_cursor(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _decode_header_value(raw: str | None) -> str:
    value = _to_str(raw).strip()
    if not value:
        return ""
    parts: list[str] = []
    for chunk, encoding in decode_header(value):
        if isinstance(chunk, bytes):
            enc = encoding or "utf-8"
            try:
                parts.append(chunk.decode(enc, errors="replace"))
            except LookupError:
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(_to_str(chunk))
    return "".join(parts).strip()


def _extract_text_body(msg: Message) -> str:
    if msg.is_multipart():
        text_parts: list[str] = []
        html_parts: list[str] = []
        for part in msg.walk():
            content_type = _to_str(part.get_content_type()).strip().lower()
            disposition = _to_str(part.get("Content-Disposition")).strip().lower()
            if "attachment" in disposition:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")
            if content_type == "text/plain":
                text_parts.append(decoded.strip())
            elif content_type == "text/html":
                html_parts.append(decoded.strip())
        if text_parts:
            return "\n\n".join(part for part in text_parts if part).strip()
        if html_parts:
            html = "\n\n".join(part for part in html_parts if part)
            html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
            html = re.sub(r"(?s)<[^>]+>", " ", html)
            html = re.sub(r"\s+", " ", html)
            return html.strip()
        return ""
    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace").strip()
    except LookupError:
        return payload.decode("utf-8", errors="replace").strip()


def _header_list(msg: Message, key: str) -> list[str]:
    out: list[str] = []
    for _, addr in getaddresses(msg.get_all(key, [])):
        text = _to_str(addr).strip()
        if text:
            out.append(text)
    return out


def _timestamp_iso(msg: Message) -> str:
    raw = _decode_header_value(msg.get("Date"))
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        return raw
    try:
        return dt.isoformat()
    except Exception:
        return raw


def _uid_from_fetch_key(fetch_key: bytes) -> int:
    text = fetch_key.decode("utf-8", errors="ignore")
    for token in text.split():
        if token.isdigit():
            return int(token)
    return 0


def fetch_rows(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    mailbox: str,
    search_query: str,
    use_ssl: bool,
    max_messages: int,
    cursor: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    if not host.strip():
        raise ValueError("Email host is required")
    if not username.strip():
        raise ValueError("Email username is required")
    if not password.strip():
        raise ValueError("Email password is required")
    if not mailbox.strip():
        raise ValueError("Email mailbox is required")
    if max_messages < 1:
        raise ValueError("max_messages must be >= 1")

    last_uid = int(cursor.get("last_uid") or 0) if isinstance(cursor, dict) else 0
    rows: list[dict[str, Any]] = []
    scanned = 0

    conn: imaplib.IMAP4 | imaplib.IMAP4_SSL
    if use_ssl:
        conn = imaplib.IMAP4_SSL(host, int(port))
    else:
        conn = imaplib.IMAP4(host, int(port))
    try:
        login_status, _ = conn.login(username, password)
        if login_status != "OK":
            raise RuntimeError("Email login failed")
        select_status, _ = conn.select(mailbox, readonly=True)
        if select_status != "OK":
            raise RuntimeError(f"Unable to select mailbox: {mailbox}")

        status, data = conn.uid("SEARCH", None, search_query)
        if status != "OK":
            raise RuntimeError(f"Email UID SEARCH failed for query: {search_query}")
        uid_values: list[int] = []
        for block in data:
            if not isinstance(block, bytes):
                continue
            for part in block.split():
                text = part.decode("utf-8", errors="ignore").strip()
                if text.isdigit():
                    uid_values.append(int(text))
        uid_values = sorted(uid for uid in uid_values if uid > last_uid)
        if max_messages > 0 and len(uid_values) > max_messages:
            uid_values = uid_values[-max_messages:]

        for uid in uid_values:
            status, msg_data = conn.uid("FETCH", str(uid), "(RFC822 FLAGS)")
            if status != "OK":
                continue
            scanned += 1
            raw_bytes = b""
            flags_text = ""
            for item in msg_data:
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                if isinstance(item[0], bytes):
                    flags_text = item[0].decode("utf-8", errors="ignore")
                payload = item[1]
                if isinstance(payload, bytes):
                    raw_bytes = payload
                    break
            if not raw_bytes:
                continue
            msg = email.message_from_bytes(raw_bytes)
            subject = _decode_header_value(msg.get("Subject"))
            from_header = _decode_header_value(msg.get("From"))
            sender_addr = _header_list(msg, "From")
            sender = sender_addr[0] if sender_addr else from_header
            to_list = _header_list(msg, "To")
            cc_list = _header_list(msg, "Cc")
            message_id = _decode_header_value(msg.get("Message-ID"))
            in_reply_to = _decode_header_value(msg.get("In-Reply-To"))
            references = _decode_header_value(msg.get("References"))
            thread_id = in_reply_to or (references.split()[-1] if references.strip() else "") or message_id or str(uid)
            body = _extract_text_body(msg)
            timestamp = _timestamp_iso(msg)

            observations = [f"source=email_imap", f"mailbox={mailbox}", f"uid={uid}"]
            if "Seen" in flags_text:
                observations.append("seen=true")
            if message_id:
                observations.append(f"message_id={message_id}")

            rows.append(
                {
                    "id": str(uid),
                    "message_id": message_id or str(uid),
                    "thread_id": thread_id,
                    "subject": subject,
                    "body": body,
                    "from": sender,
                    "to": to_list,
                    "cc": cc_list,
                    "timestamp": timestamp,
                    "observations": observations,
                }
            )

        next_last_uid = max(uid_values) if uid_values else last_uid
        next_cursor = {"version": 1, "last_uid": int(next_last_uid)}
        rows.sort(
            key=lambda row: (_to_str(row.get("timestamp")).strip(), _to_str(row.get("id")).strip()),
            reverse=True,
        )
        return rows, next_cursor, scanned
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _load_config(path: Path) -> dict[str, Any]:
    config = _read_json(path)
    username = _to_str(config.get("username")).strip()
    username_env = _to_str(config.get("username_env")).strip()
    if not username and username_env:
        username = _to_str(os.environ.get(username_env)).strip()

    password = _to_str(config.get("password")).strip()
    password_env = _to_str(config.get("password_env")).strip()
    if not password and password_env:
        password = _to_str(os.environ.get(password_env)).strip()

    return {
        "host": _to_str(config.get("host")).strip(),
        "port": int(config.get("port") or 993),
        "username": username,
        "password": password,
        "mailbox": _to_str(config.get("mailbox")).strip() or "INBOX",
        "search_query": _to_str(config.get("search_query")).strip() or "ALL",
        "use_ssl": _as_bool(config.get("use_ssl"), default=True),
        "max_messages": max(1, int(config.get("max_messages") or 200)),
    }


def load_rows(
    path: Path,
    *,
    cursor: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    cfg = _load_config(path)
    rows, next_cursor, scanned = fetch_rows(
        host=cfg["host"],
        port=cfg["port"],
        username=cfg["username"],
        password=cfg["password"],
        mailbox=cfg["mailbox"],
        search_query=cfg["search_query"],
        use_ssl=cfg["use_ssl"],
        max_messages=cfg["max_messages"],
        cursor=cursor,
    )
    return rows, scanned, next_cursor


def convert_rows(
    rows: list[dict[str, Any]],
    *,
    default_session_id: str,
    default_thread_id: str,
) -> list[dict[str, Any]]:
    converted = messages_to_turns.convert_rows(
        rows,
        default_session_id=default_session_id,
        default_thread_id=default_thread_id,
    )
    converted.sort(
        key=lambda turn: (_to_str(turn.get("timestamp")).strip(), _to_str(turn.get("turn_id")).strip()),
        reverse=True,
    )
    return converted


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pull live email via IMAP and convert to operator turns.")
    parser.add_argument("--input", required=True, help="Path to connector config JSON")
    parser.add_argument("--output", required=True, help="Path to output JSONL")
    parser.add_argument("--session-id", default="email_live", help="Default session_id")
    parser.add_argument("--thread-id", default="email-live", help="Default thread_id")
    parser.add_argument("--cursor-json", default=None, help="Optional cursor state JSON for incremental polling")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        raise FileNotFoundError(f"Input config not found: {input_path}")

    cursor_path = Path(args.cursor_json) if args.cursor_json else None
    cursor = _read_cursor(cursor_path) if cursor_path else {}
    rows, scanned, next_cursor = load_rows(input_path, cursor=cursor)
    turns = convert_rows(
        rows,
        default_session_id=_to_str(args.session_id).strip() or "email_live",
        default_thread_id=_to_str(args.thread_id).strip() or "email-live",
    )
    write_jsonl(output_path, turns)
    if cursor_path:
        _write_cursor(cursor_path, next_cursor)
    print(f"scanned_rows={scanned} output_turns={len(turns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
