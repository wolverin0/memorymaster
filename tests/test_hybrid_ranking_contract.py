"""Contrato de ranking del modo hibrido — logica pura, SIN dependencias ML.

POR QUE ESTA SEPARADO DE test_vector_search.py. Estos tests vivian ahi, y ese
archivo lleva `pytestmark = pytest.mark.ml` a nivel de modulo. CI corre
`pytest tests/ -m "not ml"`, asi que quedaron fuera de la corrida por defecto —
por compartir archivo, no por necesitar nada.

El costo fue concreto: dos regresiones de scoring del PR #223 mergearon en verde,
una de ellas descartando una claim PINEADA, que es una intencion explicita del
operador. Los tests que atrapan regresiones de ranking fueron justo los que
dejaron de correr.

EL MARCADOR `ml` DEL OTRO ARCHIVO SE QUEDA, y conviene decir por que para que
nadie lo "limpie" despues: segun pytest.ini no es por dependencias ausentes sino
porque esos archivos hacen SIGSEGV (exit 139) o se cuelgan en cargas de modelo
real cuando se MEZCLAN en la corrida completa sobre Windows. Que pasen con torch
bloqueado no contradice eso — son preguntas distintas, y confundirlas fue un
error que casi cometo al separar esto.

Estos tests no tienen ese problema: el hook vectorial es un dict y no se carga
ningun modelo. Por eso pueden correr en la matriz completa y los demas no.
"""
from __future__ import annotations

import pytest

from memorymaster.core.models import Claim
from memorymaster.recall.retrieval import rank_claim_rows


def _make_claim(
    claim_id: int,
    text: str,
    *,
    status: str = "confirmed",
    confidence: float = 0.8,
    subject: str | None = None,
    pinned: bool = False,
) -> Claim:
    return Claim(
        id=claim_id,
        text=text,
        idempotency_key=None,
        normalized_text=None,
        claim_type=None,
        subject=subject,
        predicate=None,
        object_value=None,
        scope="project",
        volatility="medium",
        status=status,
        confidence=confidence,
        pinned=pinned,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2026-03-01T00:00:00+00:00",
        updated_at="2026-03-08T00:00:00+00:00",
        last_validated_at=None,
        archived_at=None,
    )


class TestHybridRetrieval:
    def _make_vector_hook(self, scores: dict[int, float]):
        def hook(query: str, claims: list[Claim]) -> dict[int, float]:
            return {c.id: scores.get(c.id, 0.0) for c in claims}
        return hook

    def test_hybrid_without_semantic_uses_low_vector_weight(self) -> None:
        c1 = _make_claim(1, "authentication via JWT tokens")
        c2 = _make_claim(2, "database migration scripts")
        hook = self._make_vector_hook({1: 0.9, 2: 0.1})

        rows = rank_claim_rows(
            "authentication", [c1, c2],
            mode="hybrid", limit=10,
            vector_hook=hook, semantic_vectors=False,
        )
        assert len(rows) >= 1
        top = rows[0]
        # With hash vectors (10% weight), lexical should dominate
        assert top.claim.id == 1

    def test_hybrid_with_semantic_boosts_vector_weight(self) -> None:
        # c1 has no lexical match but high vector score
        c1 = _make_claim(1, "JWT bearer token validation", subject="auth")
        # c2 has lexical match but low vector score
        c2 = _make_claim(2, "search query optimization", subject="search")
        hook = self._make_vector_hook({1: 0.95, 2: 0.1})

        rows = rank_claim_rows(
            "search", [c1, c2],
            mode="hybrid", limit=10,
            vector_hook=hook, semantic_vectors=True,
        )
        # c2 should still rank high due to lexical match on "search"
        # but c1 with high vector score should also be included
        ids = [r.claim.id for r in rows]
        assert 2 in ids  # lexical match present

    def test_semantic_keeps_high_vector_no_lexical(self) -> None:
        """With semantic vectors, claims with high vector but no lexical match survive filtering."""
        c1 = _make_claim(1, "completely unrelated text about cats")
        c2 = _make_claim(2, "dogs playing in the park")
        # c1 has high vector score (semantically relevant) but no lexical overlap
        hook = self._make_vector_hook({1: 0.9, 2: 0.1})

        rows = rank_claim_rows(
            "authentication", [c1, c2],
            mode="hybrid", limit=10,
            vector_hook=hook, semantic_vectors=True,
        )
        # c1 should survive because vector_score >= 0.55 threshold
        c1_present = any(r.claim.id == 1 for r in rows)
        assert c1_present, "High vector score claim should survive even without lexical match"

    def test_non_semantic_filters_no_lexical(self) -> None:
        """Without semantic vectors, claims with no lexical match are filtered out."""
        c1 = _make_claim(1, "completely unrelated text")
        c2 = _make_claim(2, "authentication module")
        hook = self._make_vector_hook({1: 0.9, 2: 0.8})

        rows = rank_claim_rows(
            "authentication", [c1, c2],
            mode="hybrid", limit=10,
            vector_hook=hook, semantic_vectors=False,
        )
        # c1 has no lexical match and should be filtered
        ids = [r.claim.id for r in rows]
        assert 1 not in ids
        assert 2 in ids

    def test_hybrid_score_components(self) -> None:
        c = _make_claim(1, "authentication tokens", confidence=0.9)
        hook = self._make_vector_hook({1: 0.8})

        rows = rank_claim_rows(
            "authentication", [c],
            mode="hybrid", limit=10,
            vector_hook=hook, semantic_vectors=True,
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.vector_score == pytest.approx(0.8)
        assert row.lexical_score > 0
        assert row.confidence_score == pytest.approx(0.9)

        # El termino vectorial NO entra crudo: entra reescalado sobre el piso de
        # ruido. Dos textos sin relacion dan coseno ~0,5, asi que un 0,5 crudo
        # aportaria señal siendo ruido puro; VECTOR_RELEVANCE_FLOOR=0,65 mapea el
        # tramo util [0,65..1] a [0..1] y anula todo lo de abajo.
        #
        # Este assert se escribia con `0.40 * 0.8` y quedo desactualizado cuando el
        # piso se agrego en el PR #223 — el contrato cambio y su documentacion no.
        # Nadie lo vio porque el archivo esta marcado `ml` y CI corre -m "not ml".
        from memorymaster.recall.retrieval import _vector_above_floor

        vec_component = _vector_above_floor(0.8)
        assert vec_component < 0.8, (
            "el piso tiene que ATENUAR el vector crudo; si no, este test no esta "
            "verificando el reescalado sino la formula vieja"
        )
        expected = (
            (0.30 * row.lexical_score)
            + (0.20 * 0.9)
            + (0.10 * row.freshness_score)
            + (0.40 * vec_component)
        )
        assert row.score == pytest.approx(expected, abs=0.01)

    def test_legacy_mode_ignores_vector(self) -> None:
        c = _make_claim(1, "test claim")
        hook = self._make_vector_hook({1: 1.0})

        rows = rank_claim_rows(
            "test", [c],
            mode="legacy", limit=10,
            vector_hook=hook, semantic_vectors=True,
        )
        assert len(rows) == 1
        assert rows[0].vector_score == 0.0

    def test_pinned_claims_always_survive(self) -> None:
        c1 = _make_claim(1, "pinned important note", pinned=True)
        c2 = _make_claim(2, "authentication module")
        hook = self._make_vector_hook({1: 0.0, 2: 0.9})

        rows = rank_claim_rows(
            "authentication", [c1, c2],
            mode="hybrid", limit=10,
            vector_hook=hook, semantic_vectors=True,
        )
        ids = [r.claim.id for r in rows]
        assert 1 in ids, "Pinned claim should survive filtering"
