"""Static red contracts for fail-closed deployment profiles."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_postgres_compose_requires_secret_interpolation():
    compose = _read("docker-compose.postgres.yml")
    match = re.search(r"^\s*POSTGRES_PASSWORD:\s*(.+?)\s*$", compose, re.MULTILINE)

    assert match is not None
    assert "${" in match.group(1) and ":?" in match.group(1)


def test_postgres_compose_is_loopback_only_with_authenticated_healthcheck():
    compose = _read("docker-compose.postgres.yml")

    assert '"127.0.0.1:6543:5432"' in compose
    assert 'PGPASSWORD="$${POSTGRES_PASSWORD}"' in compose
    assert "psql" in compose
    assert "SELECT 1" in compose


def test_auxiliary_compose_ports_are_not_public():
    compose = _read("docker-compose.yml")
    mappings = re.findall(
        r'^\s*-\s*["\']?([^"\'\s]+:(?:6333|6334|11434))["\']?\s*$',
        compose,
        re.MULTILINE,
    )

    assert all(value.startswith(("127.0.0.1:", "localhost:")) for value in mappings)


def test_auxiliary_compose_requires_authenticated_qdrant():
    compose = _read("docker-compose.yml")

    assert "QDRANT_URL: https://qdrant:6333" in compose
    assert re.search(r"QDRANT_API_KEY:\s*[\"']?\$\{QDRANT_API_KEY:\?", compose)
    assert re.search(r"QDRANT__SERVICE__API_KEY:\s*[\"']?\$\{QDRANT_API_KEY:\?", compose)
    assert 'QDRANT__SERVICE__ENABLE_TLS: "true"' in compose
    assert "QDRANT__TLS__CERT: /qdrant/tls/cert.pem" in compose
    assert "QDRANT__TLS__KEY: /qdrant/tls/key.pem" in compose
    for variable in ("QDRANT_TLS_CERT", "QDRANT_TLS_KEY", "QDRANT_CA_CERT"):
        assert f"${{{variable}:?" in compose
    assert '--header="api-key: $${QDRANT__SERVICE__API_KEY}"' in compose
    assert "https://qdrant:6333/collections" in compose
    assert "--ca-certificate=/qdrant/tls/ca.pem" in compose


@pytest.mark.xfail(
    strict=True,
    reason="R3.4: container publishes HTTP but launches stdio MCP with a CLI-only healthcheck",
)
def test_container_entrypoint_and_healthcheck_share_an_http_contract():
    dockerfile = _read("Dockerfile")
    compose = _read("docker-compose.yml")

    assert re.search(r"CMD\s+\[.*memorymaster-(?:dashboard|http).*\]", dockerfile)
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


def test_deployment_images_reject_latest_tags():
    deployment_text = "\n".join(
        [
            _read("docker-compose.yml"),
            _read("docker-compose.postgres.yml"),
            _read("helm/memorymaster/values.yaml"),
        ]
    )

    assert not re.search(r"(?i)(?:image:\s*[^\s]+:latest|tag:\s*[\"']?latest)", deployment_text)


def test_deployment_images_are_immutable_or_required_by_digest():
    dockerfile = _read("Dockerfile")
    compose = _read("docker-compose.yml")
    postgres = _read("docker-compose.postgres.yml")
    python_image = "python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9"
    postgres_image = "postgres:16-alpine@sha256:b7587f3cb74f4f4b2a4f9d67f052edbf95eb93f4fec7c5ada3792546caaf7383"

    assert dockerfile.count(f"FROM {python_image}") == 2
    assert postgres_image in postgres
    assert 'image: "qdrant/qdrant@${QDRANT_IMAGE_DIGEST:?' in compose
    assert 'image: "ollama/ollama@${OLLAMA_IMAGE_DIGEST:?' in compose
    assert "image: memorymaster" not in compose


def test_helm_requires_digest_and_existing_qdrant_secrets():
    values = _read("helm/memorymaster/values.yaml")
    deployment = _read("helm/memorymaster/templates/deployment.yaml")

    assert re.search(r"^\s*digest:\s*[\"']{2}\s*$", values, re.MULTILINE)
    assert not re.search(r"^\s*tag:", values, re.MULTILINE)
    assert 'required "image.digest' in deployment
    assert "regexMatch" in deployment and "sha256:" in deployment
    assert 'fail "image.digest' in deployment
    assert ".Values.image.repository }}@{{" in deployment
    assert "QDRANT_API_KEY" in deployment
    assert "secretKeyRef:" in deployment
    assert ".Values.qdrant.apiKeySecret.name" in deployment
    assert ".Values.qdrant.caSecret.name" in deployment
    assert "QDRANT_CA_CERT" in deployment
    assert "readOnly: true" in deployment
    assert re.search(r"^\s*url:\s*https://", values, re.MULTILINE)
    assert 'fail "qdrant.url must use HTTPS"' in deployment


def test_helm_storage_is_coupled_to_finite_retention_envelope():
    values = _read("helm/memorymaster/values.yaml")
    pvc = _read("helm/memorymaster/templates/pvc.yaml")

    assert "size: 1Gi" not in values
    for key in (
        "verbatimMaxMi",
        "databaseMaxMi",
        "artifactsMaxMi",
        "walAndBackupHeadroomMi",
        "operatorHeadroomMi",
    ):
        assert key in values
        assert key in pvc
    assert "retention envelope" in pvc
    assert "persistence.sizeMi" in pvc


def test_postgres_smoke_requires_operator_supplied_dsn():
    script = _read("scripts/smoke_postgres.ps1")

    assert "mm_pw" not in script
    assert "$env:MEMORYMASTER_POSTGRES_DSN" in script
    assert "IsNullOrWhiteSpace" in script
    assert not re.search(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.", script)


def test_environment_example_documents_required_deployment_inputs():
    example = _read(".env.example")

    assert "# MEMORYMASTER_POSTGRES_PASSWORD=" in example
    assert "# QDRANT_API_KEY=" in example
    assert "# QDRANT_TLS_CERT=" in example
    assert "# QDRANT_TLS_KEY=" in example
    assert "# QDRANT_CA_CERT=" in example
    assert re.search(r"^# QDRANT_IMAGE_DIGEST=sha256:", example, re.MULTILINE)
    assert re.search(r"^# OLLAMA_IMAGE_DIGEST=sha256:", example, re.MULTILINE)


def test_compose_fixes_image_repositories_and_requires_digest_only_inputs():
    compose = _read("docker-compose.yml")

    assert 'image: "qdrant/qdrant@${QDRANT_IMAGE_DIGEST:?' in compose
    assert 'image: "ollama/ollama@${OLLAMA_IMAGE_DIGEST:?' in compose
    assert "QDRANT_IMAGE}" not in compose
    assert "OLLAMA_IMAGE}" not in compose


def test_helm_rejects_literal_qdrant_security_env_overrides():
    deployment = _read("helm/memorymaster/templates/deployment.yaml")

    for name in ("QDRANT_API_KEY", "QDRANT_CA_CERT", "QDRANT_URL"):
        assert f'hasKey .Values.env "{name}"' in deployment
    assert "reserved; configure it through .Values.qdrant" in deployment
