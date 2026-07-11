"""Static red contracts for fail-closed deployment profiles."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


@pytest.mark.xfail(
    strict=True,
    reason="R1.5: Postgres Compose uses a fixed default password",
)
def test_postgres_compose_requires_secret_interpolation():
    compose = _read("docker-compose.postgres.yml")
    match = re.search(r"^\s*POSTGRES_PASSWORD:\s*(.+?)\s*$", compose, re.MULTILINE)

    assert match is not None
    assert "${" in match.group(1) and ":?" in match.group(1)


@pytest.mark.xfail(
    strict=True,
    reason="R1.5: Qdrant and Ollama ports are publicly published by default",
)
def test_auxiliary_compose_ports_are_not_public():
    compose = _read("docker-compose.yml")
    mappings = re.findall(
        r'^\s*-\s*["\']?([^"\'\s]+:(?:6333|6334|11434))["\']?\s*$',
        compose,
        re.MULTILINE,
    )

    assert all(value.startswith(("127.0.0.1:", "localhost:")) for value in mappings)


@pytest.mark.xfail(
    strict=True,
    reason="R3.4: container publishes HTTP but launches stdio MCP with a CLI-only healthcheck",
)
def test_container_entrypoint_and_healthcheck_share_an_http_contract():
    dockerfile = _read("Dockerfile")
    compose = _read("docker-compose.yml")

    assert re.search(r'CMD\s+\[.*memorymaster-(?:dashboard|http).*\]', dockerfile)
    assert re.search(r"https?://(?:127\.0\.0\.1|localhost):8765/(?:healthz|readyz)", compose)
    assert '"--version"' not in compose


@pytest.mark.xfail(
    strict=True,
    reason="R3.4: Helm deployment has no liveness/readiness probes",
)
def test_helm_deployment_defines_health_and_readiness_probes():
    deployment = _read("helm/memorymaster/templates/deployment.yaml")

    assert "livenessProbe:" in deployment
    assert "readinessProbe:" in deployment
    assert "/healthz" in deployment
    assert "/readyz" in deployment


@pytest.mark.xfail(
    strict=True,
    reason="R1.5: deployment profiles use unpinned latest image tags",
)
def test_deployment_images_reject_latest_tags():
    deployment_text = "\n".join(
        [
            _read("docker-compose.yml"),
            _read("docker-compose.postgres.yml"),
            _read("helm/memorymaster/values.yaml"),
        ]
    )

    assert not re.search(r"(?i)(?:image:\s*[^\s]+:latest|tag:\s*[\"']?latest)", deployment_text)
