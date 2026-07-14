from __future__ import annotations

from importlib import resources

import memorymaster.surfaces.setup_hooks as setup


def test_setup_profiles_have_explicit_component_contracts() -> None:
    assert set(setup.SETUP_PROFILES) == {"minimal", "semantic", "team", "full-lab"}
    assert setup.SETUP_PROFILES["minimal"] == ("db", "mcp")
    assert set(setup.SETUP_PROFILES["full-lab"]) == {
        "db",
        "mcp",
        "recall_hook",
        "capture_hook",
        "provider",
        "steward",
        "vector_backend",
        "dashboard",
    }


def test_profile_aggregation_is_nonzero_for_partial_or_blocked() -> None:
    passed = {
        "db": setup.ComponentResult("db", "PASS", "ok"),
        "mcp": setup.ComponentResult("mcp", "PASS", "ok"),
    }
    assert setup.evaluate_setup_profile("minimal", passed)[0] == "PASS"
    passed["mcp"] = setup.ComponentResult("mcp", "PARTIAL", "restart required")
    assert setup.evaluate_setup_profile("minimal", passed) == ("PARTIAL", 3)
    passed["mcp"] = setup.ComponentResult("mcp", "BLOCKED", "not registered")
    assert setup.evaluate_setup_profile("minimal", passed) == ("BLOCKED", 2)


def test_explicit_profile_parser_and_packaged_hook_assets() -> None:
    args = setup.build_arg_parser().parse_args(["--profile", "semantic"])
    assert args.profile == "semantic"
    hook = resources.files("memorymaster").joinpath(
        "config_templates", "hooks", "memorymaster-session-end.py"
    )
    assert hook.is_file()
    hook_text = hook.read_text(encoding="utf-8")
    assert "memorymaster.surfaces.session_end_ingest" in hook_text
    assert "scripts.agent_session_end_ingest" not in hook_text
    assert resources.files("memorymaster").joinpath(
        "surfaces", "session_end_ingest.py"
    ).is_file()
