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


@dataclass
class RoundRobinKeyRotator:
    """Round-robin API key rotator over an explicit key list, with per-key
    cooldown on rate limits.

    Moved verbatim from ``llm_steward.KeyRotator`` (P2 phase0 cycle cut:
    ``llm_provider`` must not import ``llm_steward``). ``llm_steward``
    re-exports this class under its historical name ``KeyRotator`` for
    backward compatibility.

    Keys that receive 429 errors are placed on cooldown and skipped until
    the cooldown period expires. If all keys are on cooldown, the key with
    the earliest cooldown expiry is used (with a sleep until it becomes
    available).
    """

    keys: list[str]
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    _index: int = field(default=0, init=False, repr=False)
    _cooldowns: dict[int, float] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("KeyRotator requires at least one API key")
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for k in self.keys:
            stripped = k.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                unique.append(stripped)
        if not unique:
            raise ValueError("KeyRotator requires at least one non-empty API key")
        self.keys = unique

    @property
    def key_count(self) -> int:
        return len(self.keys)

    def get_key(self) -> str:
        """Return the next available key, skipping those on cooldown.

        If all keys are on cooldown, sleeps until the soonest one expires.
        """
        now = time.monotonic()
        # Try each key starting from current index
        for offset in range(len(self.keys)):
            idx = (self._index + offset) % len(self.keys)
            expiry = self._cooldowns.get(idx, 0.0)
            if now >= expiry:
                self._index = (idx + 1) % len(self.keys)
                return self.keys[idx]

        # All keys on cooldown: find the one that expires soonest
        soonest_idx = min(self._cooldowns, key=self._cooldowns.get)  # type: ignore[arg-type]
        wait = self._cooldowns[soonest_idx] - now
        if wait > 0:
            log.info(
                "All %d keys rate-limited; waiting %.1fs for key #%d",
                len(self.keys), wait, soonest_idx,
            )
            time.sleep(wait)
        self._index = (soonest_idx + 1) % len(self.keys)
        return self.keys[soonest_idx]

    def mark_rate_limited(self, key: str) -> None:
        """Place a key on cooldown after receiving a 429 error."""
        try:
            idx = self.keys.index(key)
        except ValueError:
            return
        expiry = time.monotonic() + self.cooldown_seconds
        self._cooldowns[idx] = expiry
        log.info(
            "Key #%d rate-limited, cooldown %.0fs (until monotonic %.1f)",
            idx, self.cooldown_seconds, expiry,
        )

    def clear_cooldown(self, key: str) -> None:
        """Remove cooldown for a key (e.g., after a successful call)."""
        try:
            idx = self.keys.index(key)
        except ValueError:
            return
        self._cooldowns.pop(idx, None)

    @property
    def available_key_count(self) -> int:
        """Number of keys not currently on cooldown."""
        now = time.monotonic()
        return sum(1 for idx in range(len(self.keys)) if now >= self._cooldowns.get(idx, 0.0))


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
