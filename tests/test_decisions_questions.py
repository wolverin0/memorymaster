"""Versioned question registry: stable hashes, literal contracts, per-surface builders."""
from __future__ import annotations

import re

import pytest

from memorymaster.decisions import questions as q
from memorymaster.decisions.config import SURFACES
from memorymaster.decisions.questions import BoundQuestion, QuestionSpec


def test_every_surface_has_registered_questions():
    surfaces = {spec.surface for spec in q.all_specs()}
    assert surfaces == set(SURFACES)


def test_sha256_is_stable_and_changes_with_wording():
    spec = q.get("recall.usable_evidence")
    again = QuestionSpec(**{**spec.__dict__})
    assert spec.sha256 == again.sha256
    reworded = QuestionSpec(**{**spec.__dict__, "instructions": spec.instructions + " Be strict."})
    assert reworded.sha256 != spec.sha256
    assert re.fullmatch(r"[0-9a-f]{64}", spec.sha256)


PINNED = {
    "hints.architecture@v1": "6b535c4489673b93f11dc87f765fe18cfa3aed6c72e636c680377040765175ed",
    "hints.bug_root_cause@v1": "1b68f77db368c2061f27f57db72f85b34c492409db92d17c7b3a48c0bc823dcc",
    "hints.constraint@v1": "47d4dede09456e6cfacb09be0ce5c17ba7a2ab13db32bef4912f9ad3913e7656",
    "hints.decision@v1": "1ea51504f762bf35004e1a0b37ee7b46e9695d22c603350af9ce2243c5b33e9b",
    "hints.environment@v1": "eaef746d59564cbb4e24e016949464ea484329c6b6e8048e662318eae9f8c2b2",
    "hints.preference@v1": "5d740804d5c0cc8b62a269104c3b704b7efee6d4d1340c89701c17c381a714b5",
    "hints.reference@v1": "8f8ac66c4a633d6ca53882f29151f3c6deeebc10fe7c9681b9ad8ccc8e9b3c17",
    "ingest.chronology@v1": "a33be46bc2a0bd265442ac5f52c4e16e78a6f12a483d0e79d67cd57776d12768",
    "ingest.evidence@v1": "9eb2fa154e4272419fbc08cc4f6e3abe54362affdd22656bcb56917587350c75",
    "ingest.modality@v1": "f2ac60a93df6900dc0f8bda0816ebb8ce12f960b7e324cc71966d8ac0dd83418",
    "ingest.novelty@v1": "1afeb8b5bd9630b2e97d75c27b8dc96f2c55794ce5cc7085702c6d49f1f17c70",
    "ingest.privacy@v1": "89168fbd61d42a41f3dec2e5f8497f8f86438dacf52d1b35bfbad498f3e7c613",
    "ingest.privacy@v2": "0cc1f8fc271f47d141411a8ecc8c2c39d4a1bac16ed8f36237a677def4791eb2",
    "ingest.scope@v1": "325b78753dbfa2a28a36ae0811c48cf990182acce26f29120f14f7d194e25d0f",
    "ingest.scope@v2": "ec7e2adf4ab4da813b73732fa4966d0ef55059d3bf1ebc4b85c3f2932b40904a",
    "ingest.specificity@v1": "4fafd7af555438e8eedd421fb1b552b5253ce756d39858fa281dd0ae7d42c49c",
    "ingest.usefulness@v1": "9582fb07be84390d9aef708d4d61429cd21f98e9a4fcf1e4a830cc102afb2d5e",
    "ingest.usefulness@v2": "97b4a03b78cbd70fef74ca7d731d41a21fafbef20e45412e190b23c364c12313",
    "lifecycle.durable@v1": "b2dd0a8fb5cbad922064a4f293e514ee7b9b681f0ee99b3c4a1d8ac666eef06e",
    "lifecycle.still_valid@v1": "a5d30d6c76fbbc203911f1ee1bbefa24c3fb3f7615911fcf1bd1e8b2850b4f6d",
    "lifecycle.useful_future@v1": "2645f04a9cac283c3695c01ab55929ecbba18e200e29a54c2ec7b1f211533884",
    "memory.contradicts@v1": "cd66d22bada0ed2adca39e38716da44738bc32f04c78674c73a8a29fdad7f9e5",
    "memory.same_fact@v1": "abd2eb54654e706d08ddfb206379671584c364c15560b37a4205870cca13dd3b",
    "memory.same_scope@v1": "f7a0b2b343cf057bf0d7dd7dbd3faf5ad82fb3df66941c8c0847cff71db33d7c",
    "memory.supersedes@v1": "e28736ae4154c9ee6e552aa8cc9e7cca0f6bc0f533631bbb1732759c65f00f4f",
    "memory.supersedes_alt@v1": "c13e837f2af2def9517c56415e110506c5a13cf3abc3065496dbede43738205b",
    "recall.contradicts_request@v1": "d45f48a00ffad0e584d1539916dafd5fd4117d783688a44a68a77a5fcea79e6e",
    "recall.instruction_like@v1": "7484687e702e2f7f5ad5b88e80fbb9ba42eabe298fe38b9998c8cde0e741c917",
    "recall.relevant@v1": "ff7791cd422df27618a8ff00e65497873497fdd18e7f30df62b3528f4c56536f",
    "recall.usable_evidence@v1": "df9eb45fde9c53b74a237ae5a71a3f84945e518a2fe31482840deaf1e0cda47e",
    "route.query_type@v1": "72dac340a0710b515a1a0ee548a9edf88c74034e47ef1c3644ad5d458df73937",
    "session.relevant_to_project@v1": "f550dc6a375989d0e6a5fb422965c2791caf6f6bbd0103daccd34bb77effc833",
    "skills.fits@v1": "019d70559791c66f2ff65814d0fbc1e36c4e157b36027c48dcc0b49368abf7f5",
    "skills.needs_procedure@v1": "a0765629e355f9036e1490ab49b91de469728a6005caeda19f7c175a4f5ff366",
    "skills.which_skill@v1": "9aa8e5f70a3b51190cbcb7b5303d5ed0893ae7046063f5506d94b37a45ca64de",
}


def test_registered_hashes_are_pinned():
    """Changing a question's wording requires a new version and a new pin here."""
    current = {spec.key: spec.sha256 for spec in q.all_specs()}
    assert current == PINNED


def test_registry_rejects_duplicate_keys():
    spec = q.get("hints.decision")
    with pytest.raises(ValueError):
        q.Registry([spec, spec])


def test_questions_are_literal_contracts():
    forbidden = re.compile(
        r"\b(?:not un|never not|how many|count|calculate|days? ago|weeks? ago|date|\d{4}-\d{2}-\d{2})\b", re.I
    )
    for spec in q.all_specs():
        assert spec.primitive in {"noul", "choice", "score"}
        assert not forbidden.search(spec.instructions), spec.key
        assert spec.instructions.count("?") == 1, f"{spec.key} must ask exactly one question"
        if spec.primitive == "score":
            levels = [level for level, _ in spec.levels]
            assert levels == sorted(levels) and len(levels) >= 3
        if spec.primitive == "choice" and not spec.dynamic_options:
            assert spec.options and any(option == "unknown" for option, _ in spec.options)


def test_contract_question_shapes():
    assert q.get("recall.relevant").primitive == "score" and len(q.get("recall.relevant").levels) == 4
    assert q.get("memory.same_fact").primitive == "score" and len(q.get("memory.same_fact").levels) == 3
    assert {s.id for s in q.specs_for("ingest")} == {f"ingest.{c}" for c in q.INGEST_CHECKS}
    assert q.INGEST_CHECKS == (
        "evidence", "chronology", "modality", "scope", "specificity", "privacy", "usefulness", "novelty"
    )
    assert {s.id for s in q.specs_for("hints")} == {
        f"hints.{label}" for label in
        ("decision", "constraint", "bug_root_cause", "environment", "reference", "architecture", "preference")
    }
    route = q.get("route.query_type")
    assert [o for o, _ in route.options] == [
        "fact_lookup", "relational", "temporal", "constraint_check", "preference", "verification", "open_ended",
        "unknown",
    ]
    assert {s.id for s in q.specs_for("dedup")} >= {"memory.supersedes", "memory.supersedes_alt"}


def test_bound_question_wire_ids_and_payload():
    spec = q.get("recall.usable_evidence")
    bound = BoundQuestion(spec, item_ref="claim:42", subject={"memory": "Use WAL mode."})
    assert bound.wire_id == "recall.usable_evidence::claim:42"
    wire = bound.wire({"memory": "Use WAL mode."})
    assert wire["type"] == "noul"
    assert wire["instructions"]["question"] == spec.instructions
    assert wire["instructions"]["memory"] == "Use WAL mode."


def test_unsafe_item_refs_are_hashed_in_wire_ids():
    bound = BoundQuestion(q.get("recall.usable_evidence"), item_ref="claim 42 / weird")
    assert re.fullmatch(r"recall\.usable_evidence::h[0-9a-f]{16}", bound.wire_id)


def test_score_and_choice_wire_criteria():
    spec = q.get("recall.relevant")
    score = BoundQuestion(spec, item_ref="claim:1").wire({"memory": "x"})
    assert score["type"] == "score"
    # Documented form: a plain list of descriptions; answers key probabilities by index into it.
    assert score["criteria"] == [description for _, description in spec.levels]
    assert all(isinstance(c, str) for c in score["criteria"])
    choice = BoundQuestion(q.get("route.query_type")).wire(None)
    assert choice["type"] == "choice" and "unknown" in choice["criteria"]
    dynamic = BoundQuestion(q.get("skills.which_skill"), options=(("none", "No skill applies."), ("skill:7", "x")))
    assert set(dynamic.wire(None)["criteria"]) == {"none", "skill:7"}
    assert dynamic.answer_schema().options == ("none", "skill:7")


def test_question_set_identity_is_order_independent():
    a = [BoundQuestion(q.get("recall.relevant"), "claim:1"), BoundQuestion(q.get("recall.usable_evidence"), "claim:1")]
    b = list(reversed(a))
    assert q.question_set_id(a) == q.question_set_id(b) == "recall.relevant@v1+recall.usable_evidence@v1"
    assert q.question_set_sha256(a) == q.question_set_sha256(b)


def test_builders_put_candidate_text_only_in_its_own_question():
    state, bound = q.build_recall(
        "how do we run tests?", "memorymaster", [("claim:1", "Run pytest with -p no:cacheprovider"), ("claim:2", "Use WAL")]
    )
    assert state == {"request": "how do we run tests?", "project": "memorymaster"}
    assert len(bound) == 8
    for item in bound:
        other = "Use WAL" if item.item_ref == "claim:1" else "Run pytest"
        assert other not in str(item.subject)
    revalidate_state, revalidate = q.build_revalidate(
        {"text": "SQLite is authoritative", "subject": "store", "predicate": "is", "object_value": "sqlite",
         "claim_type": "decision", "scope": "project:mm"},
        age_bucket="older_than_90_days",
    )
    assert revalidate_state["claim"]["age"] == "older_than_90_days"
    assert {b.spec.id for b in revalidate} == {"lifecycle.still_valid", "lifecycle.durable", "lifecycle.useful_future"}
    ingest_state, ingest = q.build_ingest("Use WAL", "we decided to use WAL", scope_label="project:mm")
    assert len(ingest) == 8 and ingest_state["candidate"] == "Use WAL"
    dedup_state, dedup = q.build_dedup("A", "B", scope_label="project:mm")
    assert set(dedup_state) == {"memory_a", "memory_b", "scope"} and len(dedup) == 5
    hints_state, hints = q.build_hints("we decided to use WAL")
    assert len(hints) == 7 and hints_state == {"prompt": "we decided to use WAL"}
    route_state, route = q.build_route("what db does pedrito use?")
    assert len(route) == 1 and route[0].spec.id == "route.query_type"
    session_state, session = q.build_session("memorymaster", [("claim:9", "text")])
    assert session_state == {"project": "memorymaster"} and session[0].subject == {"memory": "text"}


def test_revalidate_builder_rejects_raw_dates_as_age():
    with pytest.raises(ValueError):
        q.build_revalidate({"text": "x"}, age_bucket="2026-01-01")


def test_default_thresholds_cover_every_spec():
    for spec in q.all_specs():
        assert isinstance(q.default_thresholds(spec), dict)
    assert q.default_thresholds(q.get("lifecycle.still_valid"))["accept"] >= 0.8


# ------------------------------------------------- ruling R8: S3 question texts ---

PRIVACY_V2 = ("Is `candidate` free of secrets or credentials and of personal data about people other "
              "than the operator?")
USEFULNESS_V2 = "Would `candidate` help a coding agent in a future session with this operator or on this project?"
PERSONAL_SCOPE = ("Is `candidate` about the operator's own stable preferences, working style, tools, "
                  "environment or constraints?")


def test_r8_new_ingest_versions_keep_the_old_ones_for_history():
    assert (q.get("ingest.privacy").version, q.get("ingest.privacy").instructions) == (2, PRIVACY_V2)
    assert (q.get("ingest.usefulness").version, q.get("ingest.usefulness").instructions) == (2, USEFULNESS_V2)
    assert q.get("ingest.privacy", 1).instructions == "Is `candidate` free of personal data and secrets?"
    assert q.get("ingest.usefulness", 1).instructions.startswith("Would `candidate` help a coding agent")
    assert q.get("ingest.scope").version == 1  # the project question is unchanged
    personal = q.get("ingest.scope", variant="personal")
    assert (personal.id, personal.version, personal.variant, personal.instructions) == (
        "ingest.scope", 2, "personal", PERSONAL_SCOPE)
    assert q.default_thresholds(personal) == {"admit": 0.5}
    assert q.get("ingest.scope", 2) is personal
    with pytest.raises(KeyError):
        q.get("ingest.privacy", variant="personal")


def test_r8_project_candidates_ask_the_v2_texts_and_the_project_scope_question():
    _, bound = q.build_ingest("Use WAL", "we decided to use WAL", scope_label="project:mm", item_ref="cand:1")
    keys = {b.spec.id: b.spec.key for b in bound}
    assert len(bound) == 8 and len({b.wire_id for b in bound}) == 8
    assert keys["ingest.privacy"] == "ingest.privacy@v2" and keys["ingest.usefulness"] == "ingest.usefulness@v2"
    assert keys["ingest.scope"] == "ingest.scope@v1"
    assert {k for k in keys.values() if not k.endswith("@v1")} == {"ingest.privacy@v2", "ingest.usefulness@v2"}


def test_r8_personal_candidates_ask_the_personal_scope_variant():
    _, bound = q.build_ingest("The operator prefers blue interfaces", "I prefer blue interfaces",
                              scope_label="personal", item_ref="cand:2")
    scope = next(b for b in bound if b.spec.id == "ingest.scope")
    assert scope.spec.key == "ingest.scope@v2" and scope.wire(None)["instructions"] == PERSONAL_SCOPE
    explicit = q.build_ingest("x", "y", scope_label="project:mm", personal=True)[1]
    assert next(b for b in explicit if b.spec.id == "ingest.scope").spec.variant == "personal"
    assert next(b for b in q.build_ingest("x", "y", scope_label="personal", personal=False)[1]
                if b.spec.id == "ingest.scope").spec.version == 1


def test_current_specs_per_surface_are_one_version_per_question():
    ingest = q.specs_for("ingest")
    assert len(ingest) == len(q.INGEST_CHECKS) and all(spec.variant is None for spec in ingest)
    assert len(q.all_specs()) == len(PINNED)
