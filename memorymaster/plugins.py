"""Plugin system for custom validators, probes, and retrieval hooks.

Plugins register via entry points in pyproject.toml:

    [project.entry-points."memorymaster.plugins"]
    my_validator = "my_package:MyValidator"

Or programmatically:

    from memorymaster.plugins import register_plugin, PluginType
    register_plugin("my_validator", PluginType.VALIDATOR, my_validate_fn)

Plugin types:
  - VALIDATOR: Called during run-cycle to validate claims
  - PROBE: Called during steward to probe claim accuracy
  - RETRIEVAL_HOOK: Called during query to add/rerank results
  - INGESTION_HOOK: Called before/after claim ingestion
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class PluginType(str, Enum):
    VALIDATOR = "validator"
    PROBE = "probe"
    RETRIEVAL_HOOK = "retrieval_hook"
    INGESTION_HOOK = "ingestion_hook"


_registry: dict[str, dict[str, Any]] = {}


def register_plugin(
    name: str,
    plugin_type: PluginType,
    handler: Callable,
    *,
    priority: int = 100,
    description: str = "",
) -> None:
    """Register a plugin handler."""
    _registry[name] = {
        "type": plugin_type,
        "handler": handler,
        "priority": priority,
        "description": description,
    }
    logger.info("Registered plugin: %s (type=%s, priority=%d)", name, plugin_type.value, priority)


def unregister_plugin(name: str) -> bool:
    """Remove a registered plugin."""
    if name in _registry:
        del _registry[name]
        return True
    return False


def get_plugins(plugin_type: PluginType | None = None) -> list[dict[str, Any]]:
    """Get all registered plugins, optionally filtered by type."""
    plugins = []
    for name, info in sorted(_registry.items(), key=lambda x: x[1]["priority"]):
        if plugin_type is None or info["type"] == plugin_type:
            plugins.append({"name": name, **info})
    return plugins


def run_plugins(
    plugin_type: PluginType,
    *args: Any,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Run all plugins of a given type and collect results."""
    results = []
    for plugin in get_plugins(plugin_type):
        try:
            result = plugin["handler"](*args, **kwargs)
            results.append({"name": plugin["name"], "result": result, "ok": True})
        except Exception as exc:
            logger.warning("Plugin '%s' failed: %s", plugin["name"], exc)
            results.append({"name": plugin["name"], "error": str(exc), "ok": False})
    return results


def load_entry_point_plugins() -> int:
    """Load plugins from setuptools entry points."""
    loaded = 0
    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        mm_plugins = eps.get("memorymaster.plugins", []) if isinstance(eps, dict) else eps.select(group="memorymaster.plugins")
        for ep in mm_plugins:
            try:
                handler = ep.load()
                plugin_type = getattr(handler, "plugin_type", PluginType.VALIDATOR)
                priority = getattr(handler, "priority", 100)
                description = getattr(handler, "description", ep.name)
                register_plugin(ep.name, plugin_type, handler, priority=priority, description=description)
                loaded += 1
            except Exception as exc:
                logger.warning("Failed to load plugin '%s': %s", ep.name, exc)
    except Exception:
        pass  # No entry points available
    return loaded


def get_stats() -> dict[str, Any]:
    """Return plugin system statistics."""
    by_type: dict[str, int] = {}
    for info in _registry.values():
        t = info["type"].value
        by_type[t] = by_type.get(t, 0) + 1
    return {
        "total": len(_registry),
        "by_type": by_type,
        "plugins": [{"name": n, "type": i["type"].value, "priority": i["priority"]} for n, i in _registry.items()],
    }
