"""Multi-key rotator for rate-limit resilience.

Reads a gitignored `label=key` file (default: ``~/.memorymaster/gemini-keys.env``,
overridable via ``MEMORYMASTER_KEY_FILE``) and rotates keys round-robin.

Semantics:
- On 429 with ``RESOURCE_EXHAUSTED`` body: key placed on cooldown (default 60s).
- On 429/403 with ``PERMISSION_DENIED`` or ``API_KEY_INVALID``: key permanently
  skipped for this process (probably revoked).
- If all keys are on cooldown, the soonest-to-recover key is returned and the
  caller should sleep until its expiry.
- Module-level singleton so successive calls share rotation state.

Label-based format is preferred for debuggable logs; plain keys (no ``=``) also
parse — they get a synthetic label ``key1``, ``key2`` …
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SECONDS = 60.0
DEFAULT_KEY_FILE = Path.home() / ".memorymaster" / "gemini-keys.env"


@dataclass
class _KeySlot:
    label: str
    value: str
    cooldown_until: float = 0.0
    banned: bool = False


@dataclass
class KeyRotator:
    """Round-robin rotator with per-key cooldown and permanent ban.

    Thread-safe. Intended as a long-lived singleton per provider.
    """

    slots: list[_KeySlot] = field(default_factory=list)
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    _index: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __len__(self) -> int:
        return sum(1 for s in self.slots if not s.banned)

    def _now(self) -> float:
        return time.monotonic()

    def next_key(self) -> tuple[str, str] | None:
        """Return (label, key) or None if no usable keys exist.

        If all keys are on cooldown, sleeps until the soonest recovery and
        returns that key. Banned keys are skipped permanently.
        """
        with self._lock:
            usable = [s for s in self.slots if not s.banned]
            if not usable:
                return None

            now = self._now()
            for _ in range(len(usable)):
                slot = usable[self._index % len(usable)]
                self._index = (self._index + 1) % len(usable)
                if slot.cooldown_until <= now:
                    return slot.label, slot.value

            # All on cooldown — pick soonest
            soonest = min(usable, key=lambda s: s.cooldown_until)
            wait = max(0.0, soonest.cooldown_until - self._now())

        if wait > 0:
            log.info("all keys on cooldown, sleeping %.1fs for %s", wait, soonest.label)
            time.sleep(wait)
        return soonest.label, soonest.value

    def mark_rate_limited(self, label: str, retry_after: float | None = None) -> None:
        cooldown = retry_after if retry_after and retry_after > 0 else self.cooldown_seconds
        with self._lock:
            for s in self.slots:
                if s.label == label:
                    s.cooldown_until = self._now() + cooldown
                    log.info("key %s on cooldown for %.1fs (429)", label, cooldown)
                    return

    def mark_banned(self, label: str, reason: str = "") -> None:
        with self._lock:
            for s in self.slots:
                if s.label == label:
                    s.banned = True
                    log.warning("key %s permanently banned: %s", label, reason or "unknown")
                    return


def _parse_file(path: Path) -> list[_KeySlot]:
    slots: list[_KeySlot] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return slots
    anon_counter = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            label, _, value = line.partition("=")
            label = label.strip()
            value = value.strip().strip('"').strip("'")
        else:
            anon_counter += 1
            label = f"key{anon_counter}"
            value = line.strip().strip('"').strip("'")
        if not value:
            continue
        slots.append(_KeySlot(label=label, value=value))
    # Deduplicate by value, preserving first-seen label
    seen: set[str] = set()
    unique: list[_KeySlot] = []
    for s in slots:
        if s.value in seen:
            continue
        seen.add(s.value)
        unique.append(s)
    return unique


_instances: dict[str, KeyRotator] = {}
_instances_lock = threading.Lock()


def get_rotator(provider: str = "gemini") -> KeyRotator | None:
    """Return (and cache) the rotator for a provider, or None if no keys file.

    Current implementation is Gemini-only; `provider` arg reserved for future
    expansion (openai, anthropic). The cache is keyed by `provider` so multiple
    rotators can coexist.

    Env vars:
        MEMORYMASTER_KEY_FILE — absolute path to the keys file
                                 (default: ~/.memorymaster/gemini-keys.env)
    """
    cache_key = provider
    with _instances_lock:
        if cache_key in _instances:
            return _instances[cache_key]

    path_env = os.environ.get("MEMORYMASTER_KEY_FILE")
    path = Path(path_env) if path_env else DEFAULT_KEY_FILE
    if not path.is_file():
        return None

    slots = _parse_file(path)
    if not slots:
        log.warning("key file %s is empty or unparseable", path)
        return None

    rotator = KeyRotator(slots=slots)
    with _instances_lock:
        _instances.setdefault(cache_key, rotator)
        return _instances[cache_key]


def clear_cache() -> None:
    """Drop all cached rotators. Primarily for tests."""
    with _instances_lock:
        _instances.clear()
