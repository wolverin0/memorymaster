from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    from scripts import messages_to_turns
except ImportError:
    import messages_to_turns  # type: ignore[no-redef]

Requester = Callable[[str, dict[str, str]], tuple[Any, dict[str, str]]]


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


def _json_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        out[_to_str(key).lower()] = _to_str(value)
    return out


def _default_requester(url: str, headers: dict[str, str]) -> tuple[Any, dict[str, str]]:
    request = urllib.request.Request(url=url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw)
            return payload, _json_headers(dict(response.headers.items()))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = body[:300].replace("\n", " ").strip()
        raise RuntimeError(f"Slack API request failed ({exc.code}) for {url}: {detail}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Slack connector config must be a JSON object")
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


def _parse_ts(value: str) -> Decimal | None:
    text = value.strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _cursor_channel_state(cursor: dict[str, Any] | None, channel_id: str) -> tuple[str, set[str]]:
    if not isinstance(cursor, dict):
        return "", set()
    channels = cursor.get("channels")
    if not isinstance(channels, dict):
        return "", set()
    state = channels.get(channel_id)
    if not isinstance(state, dict):
        return "", set()
    latest_ts = _to_str(state.get("latest_ts")).strip()
    raw_ids = state.get("latest_ids")
    if not isinstance(raw_ids, list):
        return latest_ts, set()
    latest_ids = {_to_str(item).strip() for item in raw_ids if _to_str(item).strip()}
    return latest_ts, latest_ids


def _stream_state(latest_ts: str, latest_ids: set[str]) -> dict[str, Any]:
    return {"latest_ts": latest_ts, "latest_ids": sorted(latest_ids)[:500]}


def _build_url(base_url: str, endpoint: str, params: dict[str, str]) -> str:
    root = base_url.rstrip("/")
    query = urllib.parse.urlencode(params)
    return f"{root}/{endpoint}?{query}"


def _api_get(
    *,
    base_url: str,
    endpoint: str,
    token: str,
    params: dict[str, str],
    requester: Requester,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "memorymaster-slack-live-connector",
    }
    url = _build_url(base_url, endpoint, params)
    payload, _ = requester(url, headers)
    if not isinstance(payload, dict):
        raise ValueError(f"Slack API payload for {endpoint} must be a JSON object")
    if not bool(payload.get("ok")):
        error_text = _to_str(payload.get("error")).strip() or "unknown_error"
        raise RuntimeError(f"Slack API error for {endpoint}: {error_text}")
    return payload


def _row_id(msg: dict[str, Any]) -> str:
    return _to_str(msg.get("client_msg_id")).strip() or _to_str(msg.get("ts")).strip()


def _row_from_message(
    *,
    channel_id: str,
    channel_name: str,
    message: dict[str, Any],
    is_reply: bool,
) -> dict[str, Any]:
    ts = _to_str(message.get("ts")).strip()
    thread_ts = _to_str(message.get("thread_ts")).strip() or ts
    sender = (
        _to_str(message.get("user")).strip()
        or _to_str(message.get("username")).strip()
        or _to_str(message.get("bot_id")).strip()
    )
    subtype = _to_str(message.get("subtype")).strip()
    observations = [f"source=slack_api", f"channel_id={channel_id}", f"channel={channel_name}", f"ts={ts}"]
    if subtype:
        observations.append(f"subtype={subtype}")
    if is_reply:
        observations.append("is_reply=true")

    return {
        "message_id": _row_id(message),
        "thread_id": thread_ts,
        "channel": channel_name or channel_id,
        "sender": sender,
        "text": _to_str(message.get("text")).strip(),
        "timestamp": ts,
        "observations": observations,
    }


def _fetch_replies(
    *,
    base_url: str,
    token: str,
    channel_id: str,
    thread_ts: str,
    max_replies: int,
    requester: Requester,
) -> list[dict[str, Any]]:
    if max_replies <= 0 or not thread_ts:
        return []
    payload = _api_get(
        base_url=base_url,
        endpoint="conversations.replies",
        token=token,
        params={"channel": channel_id, "ts": thread_ts, "limit": str(max_replies)},
        requester=requester,
    )
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    out: list[dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        if _to_str(item.get("ts")).strip() == thread_ts:
            continue
        out.append(item)
    return out


def _normalize_channels(value: Any) -> list[dict[str, str]]:
    channels: list[dict[str, str]] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                channel_id = _to_str(item.get("id")).strip()
                name = _to_str(item.get("name")).strip() or channel_id
                if channel_id:
                    channels.append({"id": channel_id, "name": name})
            else:
                channel_id = _to_str(item).strip()
                if channel_id:
                    channels.append({"id": channel_id, "name": channel_id})
    return channels


def fetch_rows(
    *,
    base_url: str,
    token: str,
    channels: list[dict[str, str]],
    per_page: int = 200,
    max_pages: int = 3,
    include_replies: bool = False,
    max_replies: int = 20,
    cursor: dict[str, Any] | None = None,
    requester: Requester | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    requester_fn = requester or _default_requester
    if not base_url.strip():
        raise ValueError("Slack base_url is required")
    if not token.strip():
        raise ValueError("Slack token is required")
    if not channels:
        raise ValueError("Slack channels list is required")
    if per_page < 1:
        raise ValueError("per_page must be >= 1")
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")

    all_rows: list[dict[str, Any]] = []
    scanned = 0
    next_cursor: dict[str, Any] = {"version": 1, "channels": {}}

    for channel in channels:
        channel_id = _to_str(channel.get("id")).strip()
        channel_name = _to_str(channel.get("name")).strip() or channel_id
        if not channel_id:
            continue
        existing_ts, existing_ids = _cursor_channel_state(cursor, channel_id)
        existing_ts_num = _parse_ts(existing_ts)
        newest_ts = existing_ts
        newest_ts_num = existing_ts_num
        newest_ids: set[str] = set(existing_ids)
        should_stop = False
        page_cursor = ""

        for _ in range(max_pages):
            params = {"channel": channel_id, "limit": str(per_page)}
            if page_cursor:
                params["cursor"] = page_cursor
            payload = _api_get(
                base_url=base_url,
                endpoint="conversations.history",
                token=token,
                params=params,
                requester=requester_fn,
            )
            messages = payload.get("messages")
            if not isinstance(messages, list) or not messages:
                break

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                scanned += 1
                ts = _to_str(msg.get("ts")).strip()
                row_id = _row_id(msg)
                ts_num = _parse_ts(ts)

                if ts_num is not None:
                    if newest_ts_num is None or ts_num > newest_ts_num:
                        newest_ts = ts
                        newest_ts_num = ts_num
                        newest_ids = set()
                    if newest_ts_num is not None and ts_num == newest_ts_num and row_id:
                        newest_ids.add(row_id)

                if existing_ts_num is not None and ts_num is not None:
                    if ts_num < existing_ts_num:
                        should_stop = True
                        break
                    if ts_num == existing_ts_num and row_id and row_id in existing_ids:
                        continue

                all_rows.append(
                    _row_from_message(
                        channel_id=channel_id,
                        channel_name=channel_name,
                        message=msg,
                        is_reply=False,
                    )
                )
                if include_replies and _to_str(msg.get("reply_count")).strip():
                    thread_ts = _to_str(msg.get("thread_ts")).strip() or ts
                    replies = _fetch_replies(
                        base_url=base_url,
                        token=token,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        max_replies=max_replies,
                        requester=requester_fn,
                    )
                    for reply in replies:
                        scanned += 1
                        reply_id = _row_id(reply)
                        reply_ts = _to_str(reply.get("ts")).strip()
                        reply_ts_num = _parse_ts(reply_ts)
                        if reply_ts_num is not None:
                            if newest_ts_num is None or reply_ts_num > newest_ts_num:
                                newest_ts = reply_ts
                                newest_ts_num = reply_ts_num
                                newest_ids = set()
                            if newest_ts_num is not None and reply_ts_num == newest_ts_num and reply_id:
                                newest_ids.add(reply_id)
                        if existing_ts_num is not None and reply_ts_num is not None:
                            if reply_ts_num < existing_ts_num:
                                continue
                            if reply_ts_num == existing_ts_num and reply_id and reply_id in existing_ids:
                                continue
                        all_rows.append(
                            _row_from_message(
                                channel_id=channel_id,
                                channel_name=channel_name,
                                message=reply,
                                is_reply=True,
                            )
                        )

            if should_stop:
                break
            meta = payload.get("response_metadata") if isinstance(payload.get("response_metadata"), dict) else {}
            next_page = _to_str(meta.get("next_cursor")).strip()
            if not next_page:
                break
            page_cursor = next_page

        if newest_ts_num is not None and existing_ts_num is not None and newest_ts_num == existing_ts_num:
            newest_ids = set(existing_ids).union(newest_ids)
        next_cursor["channels"][channel_id] = _stream_state(newest_ts, newest_ids)

    all_rows.sort(
        key=lambda row: (_parse_ts(_to_str(row.get("timestamp")).strip()) or Decimal(0), _to_str(row.get("message_id")).strip()),
        reverse=True,
    )
    return all_rows, next_cursor, scanned


def _load_config(path: Path) -> dict[str, Any]:
    config = _read_json(path)
    token = _to_str(config.get("token")).strip()
    token_env = _to_str(config.get("token_env")).strip()
    if not token and token_env:
        token = _to_str(os.environ.get(token_env)).strip()

    channels = _normalize_channels(config.get("channels"))
    if not channels:
        single = _to_str(config.get("channel")).strip()
        if single:
            channels = [{"id": single, "name": single}]

    return {
        "base_url": _to_str(config.get("base_url")).strip() or "https://slack.com/api",
        "token": token,
        "channels": channels,
        "per_page": max(1, int(config.get("per_page") or 200)),
        "max_pages": max(1, int(config.get("max_pages") or 3)),
        "include_replies": _as_bool(config.get("include_replies"), default=False),
        "max_replies": max(1, int(config.get("max_replies") or 20)),
    }


def load_rows(
    path: Path,
    *,
    cursor: dict[str, Any] | None = None,
    requester: Requester | None = None,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    cfg = _load_config(path)
    rows, next_cursor, scanned = fetch_rows(
        base_url=cfg["base_url"],
        token=cfg["token"],
        channels=cfg["channels"],
        per_page=cfg["per_page"],
        max_pages=cfg["max_pages"],
        include_replies=cfg["include_replies"],
        max_replies=cfg["max_replies"],
        cursor=cursor,
        requester=requester,
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
        key=lambda turn: (
            _parse_ts(_to_str(turn.get("timestamp")).strip()) or Decimal(0),
            _to_str(turn.get("turn_id")).strip(),
        ),
        reverse=True,
    )
    return converted


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pull live Slack channel messages and convert to operator turns.")
    parser.add_argument("--input", required=True, help="Path to connector config JSON")
    parser.add_argument("--output", required=True, help="Path to output JSONL")
    parser.add_argument("--session-id", default="slack_live", help="Default session_id")
    parser.add_argument("--thread-id", default="slack-live", help="Default thread_id")
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
        default_session_id=_to_str(args.session_id).strip() or "slack_live",
        default_thread_id=_to_str(args.thread_id).strip() or "slack-live",
    )
    write_jsonl(output_path, turns)
    if cursor_path:
        _write_cursor(cursor_path, next_cursor)
    print(f"scanned_rows={scanned} output_turns={len(turns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
