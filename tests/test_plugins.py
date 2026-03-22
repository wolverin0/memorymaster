"""Tests for plugin system."""

from __future__ import annotations

import pytest

from memorymaster.plugins import (
    PluginType,
    get_plugins,
    register_plugin,
    run_plugins,
    unregister_plugin,
)


class TestPluginType:
    """Test PluginType enum."""

    def test_plugin_type_values(self):
        """PluginType has correct values."""
        assert PluginType.VALIDATOR.value == "validator"
        assert PluginType.PROBE.value == "probe"
        assert PluginType.RETRIEVAL_HOOK.value == "retrieval_hook"
        assert PluginType.INGESTION_HOOK.value == "ingestion_hook"


class TestRegisterPlugin:
    """Test plugin registration."""

    def teardown_method(self):
        """Clean up registry after each test."""
        import memorymaster.plugins as p
        p._registry.clear()

    def test_register_plugin_simple(self):
        """Register a simple plugin."""
        def my_handler():
            return "result"

        register_plugin("my_plugin", PluginType.VALIDATOR, my_handler)

        plugins = get_plugins()
        assert len(plugins) == 1
        assert plugins[0]["name"] == "my_plugin"

    def test_register_plugin_with_priority(self):
        """Register plugin with custom priority."""
        def handler1():
            pass
        def handler2():
            pass

        register_plugin("plugin1", PluginType.VALIDATOR, handler1, priority=100)
        register_plugin("plugin2", PluginType.VALIDATOR, handler2, priority=50)

        plugins = get_plugins(PluginType.VALIDATOR)
        # Should be sorted by priority (lower priority first, but stored as-is)
        assert plugins[0]["priority"] == 50
        assert plugins[1]["priority"] == 100

    def test_register_plugin_with_description(self):
        """Register plugin with description."""
        def handler():
            pass

        register_plugin(
            "documented",
            PluginType.VALIDATOR,
            handler,
            description="A test plugin",
        )

        plugins = get_plugins()
        assert plugins[0]["description"] == "A test plugin"

    def test_register_plugin_overwrites_existing(self):
        """Registering same name overwrites existing."""
        def handler1():
            return "first"
        def handler2():
            return "second"

        register_plugin("plugin", PluginType.VALIDATOR, handler1)
        register_plugin("plugin", PluginType.VALIDATOR, handler2)

        plugins = get_plugins()
        assert len(plugins) == 1
        assert plugins[0]["handler"]() == "second"


class TestUnregisterPlugin:
    """Test plugin unregistration."""

    def teardown_method(self):
        """Clean up registry after each test."""
        import memorymaster.plugins as p
        p._registry.clear()

    def test_unregister_existing_plugin(self):
        """Unregister an existing plugin."""
        register_plugin("plugin", PluginType.VALIDATOR, lambda: None)

        result = unregister_plugin("plugin")
        assert result is True
        assert len(get_plugins()) == 0

    def test_unregister_nonexistent_plugin(self):
        """Unregister non-existent plugin returns False."""
        result = unregister_plugin("nonexistent")
        assert result is False

    def test_unregister_leaves_others(self):
        """Unregistering one plugin leaves others."""
        register_plugin("plugin1", PluginType.VALIDATOR, lambda: None)
        register_plugin("plugin2", PluginType.VALIDATOR, lambda: None)

        unregister_plugin("plugin1")

        plugins = get_plugins()
        assert len(plugins) == 1
        assert plugins[0]["name"] == "plugin2"


class TestGetPlugins:
    """Test plugin retrieval."""

    def teardown_method(self):
        """Clean up registry after each test."""
        import memorymaster.plugins as p
        p._registry.clear()

    def test_get_plugins_empty(self):
        """Get plugins from empty registry."""
        result = get_plugins()
        assert result == []

    def test_get_plugins_all(self):
        """Get all plugins without filter."""
        register_plugin("validator_plugin", PluginType.VALIDATOR, lambda: None)
        register_plugin("probe_plugin", PluginType.PROBE, lambda: None)

        plugins = get_plugins()
        assert len(plugins) == 2

    def test_get_plugins_by_type(self):
        """Get plugins filtered by type."""
        register_plugin("validator1", PluginType.VALIDATOR, lambda: None)
        register_plugin("validator2", PluginType.VALIDATOR, lambda: None)
        register_plugin("probe1", PluginType.PROBE, lambda: None)

        validators = get_plugins(PluginType.VALIDATOR)
        assert len(validators) == 2
        assert all(p["type"] == PluginType.VALIDATOR for p in validators)

    def test_get_plugins_sorted_by_priority(self):
        """Get plugins sorted by priority."""
        register_plugin("p1", PluginType.VALIDATOR, lambda: None, priority=300)
        register_plugin("p2", PluginType.VALIDATOR, lambda: None, priority=100)
        register_plugin("p3", PluginType.VALIDATOR, lambda: None, priority=200)

        plugins = get_plugins(PluginType.VALIDATOR)
        priorities = [p["priority"] for p in plugins]
        assert priorities == [100, 200, 300]


class TestRunPlugins:
    """Test plugin execution."""

    def teardown_method(self):
        """Clean up registry after each test."""
        import memorymaster.plugins as p
        p._registry.clear()

    def test_run_plugins_no_plugins(self):
        """Run plugins when none registered."""
        result = run_plugins(PluginType.VALIDATOR)
        assert result == []

    def test_run_plugins_single_plugin(self):
        """Run a single plugin."""
        def my_validator(claim):
            return {"valid": True}

        register_plugin("validator", PluginType.VALIDATOR, my_validator)

        results = run_plugins(PluginType.VALIDATOR, claim="test")
        assert len(results) == 1
        assert results[0]["name"] == "validator"
        assert results[0]["ok"] is True
        assert results[0]["result"]["valid"] is True

    def test_run_plugins_multiple(self):
        """Run multiple plugins."""
        register_plugin("v1", PluginType.VALIDATOR, lambda c: {"id": 1})
        register_plugin("v2", PluginType.VALIDATOR, lambda c: {"id": 2})

        results = run_plugins(PluginType.VALIDATOR, claim="test")
        assert len(results) == 2

    def test_run_plugins_passes_arguments(self):
        """Plugin receives passed arguments."""
        def handler(claim, score):
            return {"claim": claim, "score": score}

        register_plugin("handler", PluginType.VALIDATOR, handler)

        results = run_plugins(PluginType.VALIDATOR, claim=42, score=0.8)
        assert results[0]["result"]["claim"] == 42
        assert results[0]["result"]["score"] == 0.8

    def test_run_plugins_exception_handling(self):
        """Plugin exception is caught and reported."""
        def failing_handler():
            raise ValueError("Plugin error")

        register_plugin("failing", PluginType.VALIDATOR, failing_handler)

        results = run_plugins(PluginType.VALIDATOR)
        assert len(results) == 1
        assert results[0]["ok"] is False
        assert "error" in results[0]
        assert "Plugin error" in results[0]["error"]

    def test_run_plugins_mixed_success_failure(self):
        """Some plugins succeed, some fail."""
        register_plugin("good", PluginType.VALIDATOR, lambda: {"ok": True})
        register_plugin("bad", PluginType.VALIDATOR, lambda: 1 / 0)

        results = run_plugins(PluginType.VALIDATOR)
        assert len(results) == 2

        good = [r for r in results if r["ok"]]
        bad = [r for r in results if not r["ok"]]
        assert len(good) == 1
        assert len(bad) == 1

    def test_run_plugins_by_type_only(self):
        """Only plugins of specified type are run."""
        register_plugin("v1", PluginType.VALIDATOR, lambda: "validator")
        register_plugin("p1", PluginType.PROBE, lambda: "probe")

        results = run_plugins(PluginType.VALIDATOR)
        assert len(results) == 1
        assert results[0]["result"] == "validator"


class TestPluginIntegration:
    """Integration tests."""

    def teardown_method(self):
        """Clean up registry after each test."""
        import memorymaster.plugins as p
        p._registry.clear()

    def test_register_and_run_workflow(self):
        """Full workflow: register, get, run."""
        def validator1(claim):
            return {"score": 0.8}

        def validator2(claim):
            return {"score": 0.9}

        register_plugin("v1", PluginType.VALIDATOR, validator1, priority=100)
        register_plugin("v2", PluginType.VALIDATOR, validator2, priority=50)

        # Get validators sorted by priority
        plugins = get_plugins(PluginType.VALIDATOR)
        assert len(plugins) == 2
        assert plugins[0]["name"] == "v2"  # priority 50 comes first

        # Run validators
        results = run_plugins(PluginType.VALIDATOR, claim="test")
        assert len(results) == 2
        assert all(r["ok"] for r in results)

    def test_multiple_plugin_types(self):
        """Register and run different plugin types."""
        register_plugin("val", PluginType.VALIDATOR, lambda: "validator")
        register_plugin("prb", PluginType.PROBE, lambda: "probe")
        register_plugin("ret", PluginType.RETRIEVAL_HOOK, lambda: "retrieval")

        assert len(get_plugins(PluginType.VALIDATOR)) == 1
        assert len(get_plugins(PluginType.PROBE)) == 1
        assert len(get_plugins(PluginType.RETRIEVAL_HOOK)) == 1
        assert len(get_plugins()) == 3
