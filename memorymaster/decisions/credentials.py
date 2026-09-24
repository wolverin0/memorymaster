"""TypeSafe API key lookup that never exposes the value.

Order: ``TYPESAFE_API_KEY`` in the process environment, then (Windows only) the
user's persistent ``HKCU\\Environment`` value, so a long-lived process started
before the key was set still finds it.  The key is wrapped in :class:`ApiKey`,
whose ``repr``/``str`` never render it; it is never logged, never placed in argv
and never included in exception text.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Mapping

API_KEY_ENV = "TYPESAFE_API_KEY"
_REGISTRY_PATH = "Environment"
_log = logging.getLogger(__name__)


class CredentialError(RuntimeError):
    """Raised for an unusable key; the message never contains the value."""


class ApiKey:
    """Opaque holder for the bearer secret."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        cleaned = value.strip() if isinstance(value, str) else ""
        if not cleaned:
            raise CredentialError("empty API key")
        self._value = cleaned

    def reveal(self) -> str:
        return self._value

    def bearer_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._value}"}

    def scrub(self, text: str) -> str:
        """Remove the key from arbitrary text before it is logged or stored."""
        return text.replace(self._value, "[REDACTED:api_key]") if isinstance(text, str) else text

    def __repr__(self) -> str:
        return "ApiKey(***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __reduce__(self):  # never pickle into a child process or a log record
        raise CredentialError("ApiKey is not serializable")


def _read_user_environment(name: str, winreg_module: Any | None) -> str | None:
    module = winreg_module
    if module is None:
        if not sys.platform.startswith("win"):
            return None
        try:
            import winreg as module  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        with module.OpenKey(module.HKEY_CURRENT_USER, _REGISTRY_PATH, 0, module.KEY_READ) as key:
            value, _kind = module.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    except Exception as exc:  # never echo the exception text: it may contain the value
        _log.debug("user environment lookup failed (%s)", type(exc).__name__)
        return None
    return value if isinstance(value, str) else None


def get_api_key(environ: Mapping[str, str] | None = None, *, winreg_module: Any | None = None) -> ApiKey | None:
    """Return the key or ``None`` when it is not configured anywhere."""
    source = os.environ if environ is None else environ
    raw = source.get(API_KEY_ENV)
    if not (isinstance(raw, str) and raw.strip()):
        raw = _read_user_environment(API_KEY_ENV, winreg_module)
    if isinstance(raw, str) and raw.strip():
        return ApiKey(raw)
    return None


__all__ = ["API_KEY_ENV", "ApiKey", "CredentialError", "get_api_key"]
