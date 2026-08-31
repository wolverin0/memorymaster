"""R4.4 single-source release and publication contracts."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
import subprocess
import sys

import memorymaster
from memorymaster.surfaces.dashboard import DashboardRequestHandler
from scripts.generate_release_truth import _source_console_entrypoints, _source_test_function_count


ROOT = Path(__file__).resolve().parents[1]


def _project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None
    return match.group(1)


def _dashboard_html() -> str:
    handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
    handler.wfile = BytesIO()
    handler.send_response = lambda *_args, **_kwargs: None
    handler.send_header = lambda *_args, **_kwargs: None
    handler.end_headers = lambda *_args, **_kwargs: None
    handler._write_dashboard()
    return handler.wfile.getvalue().decode("utf-8")


def test_package_and_dashboard_derive_the_pyproject_version() -> None:
    version = _project_version()
    assert memorymaster.__version__ == version
    assert f'<span class="version">v{version}</span>' in _dashboard_html()


def test_install_probe_uses_an_explicit_importlib_util_import() -> None:
    source = (ROOT / "memorymaster/surfaces/setup_detect.py").read_text(encoding="utf-8")
    assert "from importlib.util import find_spec" in source
    assert 'find_spec("memorymaster")' in source


def test_install_probe_survives_a_clean_process_without_importlib_util_attribute() -> None:
    code = (
        "from memorymaster.surfaces import setup_detect; "
        "import importlib; "
        "delattr(importlib, 'util'); "
        "raise SystemExit(0 if setup_detect._probe_mm_installed() else 1)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=False)
    assert result.returncode == 0


def test_generated_release_truth_is_committed_and_current() -> None:
    """The generated files must match the repository, not merely exist.

    This asserted only ``is_file()`` on both paths until 2026-08-18, so it
    passed against a release-truth generated six months earlier and reported
    "current" while saying nothing about currency. The gap was not theoretical:
    a change adding four test functions ran the full local suite green and was
    then rejected by CI, which runs the real check. Now both run the same one.
    """
    script = ROOT / "scripts/generate_release_truth.py"
    generated = ROOT / "docs/generated/release-truth.md"
    assert script.is_file()
    assert generated.is_file()

    result = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "generated release truth is stale — run "
        "`python scripts/generate_release_truth.py`\n"
        f"{result.stdout}{result.stderr}"
    )


def test_release_truth_test_inventory_is_platform_independent(tmp_path: Path) -> None:
    (tmp_path / "test_alpha.py").write_bytes(
        b"def test_top_level():\r\n    pass\r\n\r\n"
        b"class TestGroup:\r\n    def test_method(self):\r\n        pass\r\n"
    )
    (tmp_path / "beta_test.py").write_bytes(
        b"async def test_async_case():\n    pass\n"
    )
    (tmp_path / "helper.py").write_bytes(b"def test_not_collected():\n    pass\n")

    assert _source_test_function_count(tmp_path) == 3


def test_release_truth_console_entrypoints_come_from_source_checkout() -> None:
    entrypoints = _source_console_entrypoints()
    assert "memorymaster-workflow-hook" in entrypoints
    assert entrypoints == sorted(entrypoints)


def test_only_root_roadmap_is_authoritative() -> None:
    roadmap = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    for heading in ("## Now", "## Next", "## Later", "## Not planned"):
        assert heading in roadmap
    for relative in ("ROADMAP-v3.2.md", "roadmapres.md", "docs/ROADMAP.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "historical" in text.lower()
        assert "ROADMAP.md" in text
        assert len(text.splitlines()) <= 12


def test_publish_requires_the_verified_downloaded_artifact() -> None:
    workflow = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    assert "verify-artifact:" in workflow
    assert "needs: verify-artifact" in workflow
    assert workflow.count("name: verified-dist") == 2
    assert "tests/test_qrels_regression.py" in workflow
    assert "tests/test_release_truth.py" in workflow
    assert "scripts/eval_memorymaster.py" not in workflow
    assert "/tmp/memorymaster-minimal" in workflow
    assert "/tmp/memorymaster-mcp" in workflow
    assert '"${WHEEL}[mcp,security]"' in workflow
    assert 'pip install -e ".[dev,mcp,security,postgres]"' in workflow


def test_minimal_cli_import_does_not_require_optional_qdrant_client() -> None:
    code = r'''
import importlib.abc
import sys

class BlockHttpx(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "httpx" or fullname.startswith("httpx."):
            raise ModuleNotFoundError("blocked optional dependency")
        return None

sys.meta_path.insert(0, BlockHttpx())
from memorymaster.surfaces.cli import build_parser
build_parser()
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=False)
    assert result.returncode == 0


def test_ci_blocks_on_generated_release_truth_drift() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "generate_release_truth.py --check" in workflow
    assert 'pip install -e ".[dev,mcp,security,postgres]"' in workflow
    assert 'pip install -e ".[dev,mcp,postgres]"' in workflow
    assert 'pytest tests/ -m "not ml" -q --tb=short' in workflow


def test_mcp_extra_excludes_the_breaking_v2_sdk() -> None:
    source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"^mcp\s*=\s*\[(.*?)\]", source, re.MULTILINE | re.DOTALL)
    assert match is not None
    assert '"mcp>=1.8.1,<2"' in match.group(1)


def test_dev_extra_installs_supply_chain_contract_runtime() -> None:
    source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"^dev\s*=\s*\[(.*?)\]", source, re.MULTILINE | re.DOTALL)
    assert match is not None
    assert '"pip-audit>=' in match.group(1)
