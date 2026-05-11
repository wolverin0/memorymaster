from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memorymaster.jobs.dedup import find_duplicates
from memorymaster.models import Claim


class CountingProvider:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.texts.append(text)
        return [1.0, 0.0]


def _claim(claim_id: int, *, scope: str, created_at: str) -> Claim:
    return Claim(
        id=claim_id,
        text=f"claim {claim_id}",
        idempotency_key=None,
        normalized_text=None,
        claim_type="fact",
        subject=None,
        predicate=None,
        object_value=None,
        scope=scope,
        volatility="medium",
        status="candidate",
        confidence=0.5,
        pinned=False,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at=created_at,
        updated_at=created_at,
        last_validated_at=None,
        archived_at=None,
    )


def _claims(count: int, *, scope: str = "project:foo") -> list[Claim]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    claims = []
    for idx in range(count):
        created_at = (start + timedelta(minutes=idx)).isoformat()
        claims.append(_claim(idx + 1, scope=scope, created_at=created_at))
    return claims


def _install_fake_service(monkeypatch, claims: list[Claim]):
    import memorymaster.cli as cli

    class FakeService:
        provider: CountingProvider | None = None

        def __init__(self, *args, **kwargs) -> None:
            pass

        def dedup(
            self,
            *,
            threshold: float = 0.92,
            min_text_overlap: float = 0.3,
            dry_run: bool = False,
            limit: int | None = None,
            scope_filter: str | None = None,
        ) -> dict:
            provider = CountingProvider()
            FakeService.provider = provider
            pairs = find_duplicates(
                claims,
                provider,
                threshold=threshold,
                min_text_overlap=min_text_overlap,
                limit=limit,
                scope_filter=scope_filter,
            )
            return {
                "scanned": len(provider.texts),
                "duplicates_found": len(pairs),
                "claims_archived": 0,
                "dry_run": dry_run,
                "threshold": threshold,
                "pairs": [],
            }

    monkeypatch.setattr(cli, "MemoryService", FakeService)
    return cli, FakeService


def test_dedup_limit_processes_only_oldest_five_claims(monkeypatch):
    claims = list(reversed(_claims(100)))
    cli, fake_service = _install_fake_service(monkeypatch, claims)

    rc = cli.main(["--db", "unused.db", "dedup", "--limit", "5", "--dry-run"])

    assert rc == 0
    assert fake_service.provider is not None
    assert fake_service.provider.texts == [
        "claim 1",
        "claim 2",
        "claim 3",
        "claim 4",
        "claim 5",
    ]


def test_dedup_scope_skips_claims_from_other_scopes(monkeypatch):
    claims = [
        *_claims(3, scope="project:foo"),
        *_claims(4, scope="project:bar"),
    ]
    cli, fake_service = _install_fake_service(monkeypatch, claims)

    rc = cli.main(["--db", "unused.db", "dedup", "--scope", "project:foo", "--dry-run"])

    assert rc == 0
    assert fake_service.provider is not None
    assert fake_service.provider.texts == ["claim 1", "claim 2", "claim 3"]
