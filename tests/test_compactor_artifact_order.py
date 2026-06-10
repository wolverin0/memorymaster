from __future__ import annotations

import pytest

from memorymaster.govern.jobs import compactor
from memorymaster.lifecycle import transition_claim
from memorymaster.models import CitationInput
from memorymaster.stores.storage import SQLiteStore


def test_compactor_does_not_archive_claims_when_artifact_write_fails(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "memory.db")
    store.init_db()
    claim = store.create_claim(
        text="The deployment target is production",
        citations=[CitationInput(source="session://test", locator="turn-1", excerpt="prod target")],
        subject="deployment",
        predicate="target",
        object_value="production",
    )
    confirmed = transition_claim(
        store,
        claim.id,
        to_status="confirmed",
        reason="test confirmation",
        event_type="validator",
    )
    monkeypatch.setattr(store, "find_for_compaction", lambda retain_days: [confirmed])

    def fail_write(*_args, **_kwargs) -> None:
        raise RuntimeError("artifact write failed")

    monkeypatch.setattr(compactor, "_write_json", fail_write)

    with pytest.raises(RuntimeError, match="artifact write failed"):
        compactor.run(store, retain_days=30, event_retain_days=36500, artifacts_dir=tmp_path / "artifacts")

    persisted = store.get_claim(claim.id, include_citations=False)
    assert persisted is not None
    assert persisted.status == "confirmed"
