"""R4.4 single-source release and publication contracts."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
import subprocess
import sys

import memorymaster
from memorymaster.surfaces.dashboard import DashboardRequestHandler


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
    script = ROOT / "scripts/generate_release_truth.py"
    generated = ROOT / "docs/generated/release-truth.md"
    assert script.is_file()
    assert generated.is_file()


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


def test_ci_blocks_on_generated_release_truth_drift() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "generate_release_truth.py --check" in workflow
