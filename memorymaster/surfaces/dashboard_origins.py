"""Exact browser-origin and HTTP authority policy, independent of token mode."""

from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlsplit

Origin = tuple[str, str, int]
WILDCARD_HOSTS = {"", "0.0.0.0", "::"}


def normalize_origin(value: str, *, referer: bool = False) -> Origin:
    if not value or value == "null" or any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        raise ValueError("invalid origin")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("invalid origin authority")
    if not referer and (parsed.path or "?" in value or "#" in value):
        raise ValueError("origin must not contain path, query or fragment")
    host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if "%" in host or not host:
        raise ValueError("invalid origin hostname")
    try:
        host = ipaddress.ip_address(host).compressed
    except ValueError:
        if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in host.split(".")):
            raise ValueError("invalid origin hostname") from None
    port = parsed.port
    if parsed.netloc.endswith(":") or port == 0:
        raise ValueError("invalid origin port")
    return parsed.scheme.lower(), host, port or (443 if parsed.scheme.lower() == "https" else 80)


def is_loopback(host: str) -> bool:
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def explicit_origins() -> frozenset[Origin]:
    raw = os.environ.get("MEMORYMASTER_DASHBOARD_ALLOWED_ORIGINS", "")
    if not raw.strip():
        return frozenset()
    return frozenset(normalize_origin(value.strip()) for value in raw.split(","))


def allowed_origins(host: str, port: int) -> frozenset[Origin]:
    explicit = explicit_origins()
    if host in WILDCARD_HOSTS:
        if not explicit:
            raise ValueError("Wildcard bind requires MEMORYMASTER_DASHBOARD_ALLOWED_ORIGINS")
        return explicit
    hosts = {host}
    if is_loopback(host):
        hosts.update({"localhost", "127.0.0.1", "::1"})
    urls = [f"http://{'[' + name + ']' if ':' in name else name}:{port}" for name in hosts]
    return explicit | frozenset(normalize_origin(url) for url in urls)


def host_allowed(value: str, allowed: frozenset[Origin]) -> bool:
    try:
        return any(normalize_origin(f"{scheme}://{value}") == (scheme, host, port) for scheme, host, port in allowed)
    except (ValueError, UnicodeError):
        return False


def single_header(headers, name: str) -> str | None:
    if hasattr(headers, "get_all"):
        values = headers.get_all(name, [])
        if len(values) > 1:
            raise ValueError("duplicate browser security header")
        return values[0] if values else None
    return headers.get(name) if headers else None
