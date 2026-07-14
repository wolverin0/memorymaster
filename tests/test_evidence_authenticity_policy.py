from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.bridges import atlas_llm_extractor
from memorymaster.bridges.action_exporters import export_approved_actions
from memorymaster.bridges.action_extractor import propose_actions_from_evidence
from memorymaster.bridges.atlas_claim_extractor import extract_atlas_claims_from_evidence
from memorymaster.bridges.media_processing import (
    EvidenceResult,
    MockTranscriptionProvider,
    process_transcription,
)
from memorymaster.bridges.media_providers import get_transcription_provider
from memorymaster.core.service import MemoryService
from memorymaster.surfaces.cli import main


@pytest.fixture()
def service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "atlas.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _audio_item(service: MemoryService, tmp_path: Path, *, item_id: str = "voice-1"):
    media_path = tmp_path / f"{item_id}.ogg"
    media_path.write_bytes(b"test audio")
    source = service.upsert_external_source(source_type="test", display_name="test")
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id=item_id,
        item_type="audio",
        payload_json={"media_path": str(media_path)},
    )
    return item, media_path


def _enable_test_media(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_MEDIA_MODE", "test")
    monkeypatch.setenv("MEMORYMASTER_ALLOW_SYNTHETIC_MEDIA", "1")


def test_mock_media_fails_closed_without_explicit_nonproduction_opt_in(
    service: MemoryService,
    tmp_path: Path,
) -> None:
    item, media_path = _audio_item(service, tmp_path)
    provider = MockTranscriptionProvider({str(media_path): "Please call the client tomorrow"})

    outcome = process_transcription(service, item.id, provider)

    assert outcome.created is False
    assert outcome.evidence is None
    assert outcome.error is not None
    assert "MEMORYMASTER_MEDIA_MODE" in outcome.error
    assert service.list_evidence_items(source_item_id=item.id) == []


def test_mock_media_is_conspicuously_enabled_for_tests(
    service: MemoryService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_test_media(monkeypatch)
    item, media_path = _audio_item(service, tmp_path)
    provider = MockTranscriptionProvider({str(media_path): "Please call the client tomorrow"})

    outcome = process_transcription(service, item.id, provider)

    assert outcome.created is True
    assert outcome.evidence is not None
    assert outcome.evidence.provider == "mock-transcription"


def test_synthetic_evidence_cannot_create_claims_or_actions(
    service: MemoryService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_test_media(monkeypatch)
    item, media_path = _audio_item(service, tmp_path)
    outcome = process_transcription(
        service,
        item.id,
        MockTranscriptionProvider({str(media_path): "Please send the installation quote tomorrow"}),
    )
    assert outcome.evidence is not None

    claims = extract_atlas_claims_from_evidence(service, scope="project:test")
    actions = propose_actions_from_evidence(service)

    assert claims.ingested == 0
    assert actions.created == 0


def test_synthetic_evidence_never_reaches_llm(
    service: MemoryService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_test_media(monkeypatch)
    item, media_path = _audio_item(service, tmp_path)
    outcome = process_transcription(
        service,
        item.id,
        MockTranscriptionProvider({str(media_path): "Remember that Acme chose SQLite"}),
    )
    assert outcome.evidence is not None

    def _unexpected_llm_call(*args, **kwargs):
        raise AssertionError("synthetic evidence reached the LLM")

    monkeypatch.setattr(atlas_llm_extractor, "_call_llm_safe", _unexpected_llm_call)
    result = atlas_llm_extractor.extract_atlas_claims_llm(
        service,
        scope="project:test",
        dry_run=True,
    )

    assert result.ingested == 0
    assert result.emitted == 0


def test_approved_action_backed_by_synthetic_evidence_is_not_exported(
    service: MemoryService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_test_media(monkeypatch)
    item, media_path = _audio_item(service, tmp_path)
    outcome = process_transcription(
        service,
        item.id,
        MockTranscriptionProvider({str(media_path): "Please pay the bill"}),
    )
    assert outcome.evidence is not None
    proposal = service.create_action_proposal(
        proposal_type="task",
        title="Pay the bill",
        source_item_id=item.id,
        evidence_item_id=outcome.evidence.id,
        destination="super-productivity",
        confidence=0.9,
        idempotency_key="synthetic-export-guard",
    )
    service.update_action_proposal_status(proposal.id, status="approved")
    output_path = tmp_path / "actions.json"

    result = export_approved_actions(service, output_path)

    assert result.exported == 0
    assert json.loads(output_path.read_text(encoding="utf-8"))["tasks"] == []
    assert [row.id for row in service.list_action_proposals(status="approved")] == [proposal.id]


def test_real_provider_reprocesses_when_only_mock_evidence_exists(
    service: MemoryService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_test_media(monkeypatch)
    item, media_path = _audio_item(service, tmp_path)
    mock = process_transcription(
        service,
        item.id,
        MockTranscriptionProvider({str(media_path): "Mock transcript"}),
    )
    assert mock.created is True
    monkeypatch.delenv("MEMORYMASTER_ALLOW_SYNTHETIC_MEDIA")
    monkeypatch.setenv("MEMORYMASTER_MEDIA_MODE", "production")

    class LocalRealProvider:
        provider_name = "local-real-test"

        def transcribe(self, path: str) -> EvidenceResult:
            return EvidenceResult(
                evidence_type="transcript",
                text="Authentic transcript",
                provider=self.provider_name,
                media_path=path,
                confidence=0.8,
            )

    real = process_transcription(service, item.id, LocalRealProvider())

    assert real.created is True
    assert real.evidence is not None
    assert real.evidence.provider == "local-real-test"
    assert len(service.list_evidence_items(source_item_id=item.id, evidence_type="transcript")) == 2


def test_provider_factory_rejects_mock_in_production() -> None:
    with pytest.raises(ValueError, match="MEMORYMASTER_MEDIA_MODE"):
        get_transcription_provider("mock")


def test_provider_factory_requires_real_provider_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        get_transcription_provider("openai")


def test_cli_requires_explicit_provider(tmp_path: Path) -> None:
    db_path = tmp_path / "atlas.db"
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()
    item, _ = _audio_item(service, tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        main([
            "--db",
            str(db_path),
            "--workspace",
            str(tmp_path),
            "transcribe-source-item",
            "--source-item-id",
            str(item.id),
        ])

    assert exc_info.value.code != 0
    assert service.list_evidence_items(source_item_id=item.id) == []
