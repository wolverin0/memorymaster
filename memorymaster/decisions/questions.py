"""Versioned registry of Jev questions and per-surface builders.

Each question is a direct, literal contract (jev-1.13 jaggedness): one narrow
judgment, no dates, arithmetic or counting, no double negation.  Ages and other
computed facts are passed as named buckets.  Candidate text goes only into its
own question's ``subject``; the shared ``state`` never mixes several memories.

Changing a question's wording or criteria changes its ``sha256``; that requires
a new ``version`` (thresholds are never inherited across versions).  Old versions
stay registered for history.  A ``variant`` is a wording of the same question for
a different kind of subject (``ingest.scope`` for ``personal`` candidates, ruling
R8): it has its own version number and thresholds, answers under the same
question id, and is only used when asked for by name.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

PRIMITIVES = ("noul", "choice", "score")
INGEST_CHECKS = ("evidence", "chronology", "modality", "scope", "specificity", "privacy", "usefulness", "novelty")
HINT_LABELS = ("decision", "constraint", "bug_root_cause", "environment", "reference", "architecture", "preference")
ROUTE_TYPES = (
    ("fact_lookup", "The request asks for a specific fact."),
    ("relational", "The request asks what depends on, uses or connects to something."),
    ("temporal", "The request asks what changed or when something happened."),
    ("constraint_check", "The request asks which rules or limits apply."),
    ("preference", "The request asks how the user prefers work to be done."),
    ("verification", "The request asks whether a statement is true."),
    ("open_ended", "The request asks for a broad explanation or overview."),
    ("unknown", "None of the other types clearly fits the request."),
)
AGE_BUCKETS = frozenset({
    "younger_than_7_days", "7_to_30_days", "30_to_90_days", "older_than_90_days", "unknown",
})
_SAFE_REF = re.compile(r"[A-Za-z0-9_.:-]{1,80}")


@dataclass(frozen=True)
class QuestionSpec:
    id: str
    version: int
    primitive: str
    surface: str
    instructions: str
    options: tuple[tuple[str, str], ...] = ()
    levels: tuple[tuple[int, str], ...] = ()
    dynamic_options: bool = False
    thresholds: Mapping[str, float] = field(default_factory=dict, compare=False, hash=False)
    variant: str | None = None

    def __post_init__(self) -> None:
        if self.primitive not in PRIMITIVES:
            raise ValueError(f"unknown primitive {self.primitive!r}")
        if self.primitive == "score" and len(self.levels) < 2:
            raise ValueError("score questions need levels")
        if self.primitive == "choice" and not (self.options or self.dynamic_options):
            raise ValueError("choice questions need options")

    @property
    def key(self) -> str:
        return f"{self.id}@v{self.version}"

    def criteria(self, options: Sequence[tuple[str, str]] | None = None) -> Any:
        """Wire criteria: ``{option: description}`` for Choice; for Score the documented
        plain list of descriptions in level order (answers key it by 0-based index)."""
        if self.primitive == "choice":
            return {option: description for option, description in (options or self.options)}
        if self.primitive == "score":
            return [description for _, description in self.levels]
        return None

    def canonical(self) -> dict[str, Any]:
        # The contract keeps each Score description with its level (hash identity);
        # the wire list is derived from it.
        criteria = ([{"level": level, "description": description} for level, description in self.levels]
                    if self.primitive == "score" else self.criteria())
        canonical: dict[str, Any] = {
            "id": self.id,
            "version": self.version,
            "primitive": self.primitive,
            "instructions": self.instructions,
            "criteria": criteria,
            "dynamic_options": self.dynamic_options,
        }
        if self.variant is not None:  # absent for default wordings: their pinned hashes do not move
            canonical["variant"] = self.variant
        return canonical

    @property
    def sha256(self) -> str:
        encoded = json.dumps(self.canonical(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AnswerSchema:
    primitive: str
    options: tuple[str, ...] = ()
    levels: tuple[int, ...] = ()


@dataclass(frozen=True)
class BoundQuestion:
    """A spec bound to one item (``item_ref=""`` for decision-level questions)."""

    spec: QuestionSpec
    item_ref: str = ""
    subject: Mapping[str, Any] | None = None
    options: tuple[tuple[str, str], ...] | None = None

    @property
    def wire_id(self) -> str:
        if not self.item_ref:
            return self.spec.id
        ref = self.item_ref
        if not _SAFE_REF.fullmatch(ref):
            ref = "h" + hashlib.sha256(ref.encode("utf-8")).hexdigest()[:16]
        return f"{self.spec.id}::{ref}"

    def effective_options(self) -> tuple[tuple[str, str], ...]:
        return tuple(self.options) if self.options is not None else self.spec.options

    def answer_schema(self) -> AnswerSchema:
        return AnswerSchema(
            primitive=self.spec.primitive,
            options=tuple(option for option, _ in self.effective_options()),
            levels=tuple(level for level, _ in self.spec.levels),
        )

    def wire(self, subject: Mapping[str, Any] | None) -> dict[str, Any]:
        """Wire form; ``subject`` must already be egress-redacted by the caller."""
        instructions: Any = self.spec.instructions
        if subject:
            instructions = {"question": self.spec.instructions, **dict(subject)}
        payload: dict[str, Any] = {"type": self.spec.primitive, "instructions": instructions}
        criteria = self.spec.criteria(self.effective_options() if self.spec.primitive == "choice" else None)
        if criteria is not None:
            payload["criteria"] = criteria
        return payload


PERSONAL_SCOPE_LABEL = "personal"


def _noul(qid: str, surface: str, text: str, *, version: int = 1, variant: str | None = None,
          **thresholds: float) -> QuestionSpec:
    return QuestionSpec(qid, version, "noul", surface, text, thresholds=thresholds, variant=variant)


_SPECS: tuple[QuestionSpec, ...] = (
    # S1 REVALIDATE — per stale claim.
    _noul("lifecycle.still_valid", "revalidate",
          "Is `claim` still a correct statement about the project or environment it describes?",
          accept=0.85, low=0.2),
    _noul("lifecycle.durable", "revalidate",
          "Is `claim` a lasting fact, decision, rule or preference rather than a description of a passing situation?",
          accept=0.6),
    _noul("lifecycle.useful_future", "revalidate",
          "Would `claim` help a coding agent do future work on this project?",
          accept=0.7, low=0.2),
    # S2 RECALL — per authorized candidate, one request per prompt.
    QuestionSpec(
        "recall.relevant", 1, "score", "recall",
        "How relevant is `memory` to `request`?",
        levels=(
            (1, "The memory is unrelated to the request."),
            (2, "The memory shares a topic with the request but does not help with it."),
            (3, "The memory helps with part of the request."),
            (4, "The memory directly answers or constrains the request."),
        ),
    ),
    _noul("recall.usable_evidence", "recall",
          "Does `memory` contain specific information that can be used to act on `request`?",
          include=0.5, k_min=2, k_max=5),
    _noul("recall.contradicts_request", "recall",
          "Does `memory` state something that conflicts with what `request` assumes or asks for?",
          label=0.7),
    _noul("recall.instruction_like", "recall",
          "Is `memory` written as a command addressed to the reader rather than as a statement of fact?",
          label=0.7),
    # S3 INGEST — eight checks mirroring dreaming.source_review.CHECKS.
    _noul("ingest.evidence", "ingest", "Is `candidate` directly supported by `evidence`?", admit=0.5),
    _noul("ingest.chronology", "ingest",
          "Does `candidate` describe its subject without relying on relative time words such as today, now or recently?",
          admit=0.5),
    _noul("ingest.modality", "ingest",
          "Does `candidate` report something that happened or was decided, rather than a plan, guess or question?",
          admit=0.5),
    _noul("ingest.scope", "ingest", "Does `candidate` apply to the project or workspace named in `scope`?", admit=0.5),
    _noul("ingest.specificity", "ingest",
          "Does `candidate` name the specific component, setting, tool or decision it is about?", admit=0.5),
    _noul("ingest.privacy", "ingest", "Is `candidate` free of personal data and secrets?", admit=0.5),
    _noul("ingest.usefulness", "ingest",
          "Would `candidate` help a coding agent in a future session on this project?", admit=0.5),
    _noul("ingest.novelty", "ingest",
          "Does `candidate` add information beyond what a software engineer already knows in general?", admit=0.5),
    # Ruling R8: v1 privacy/usefulness held operator preferences as "personal data"
    # and "not about this project"; personal candidates get their own scope wording.
    _noul("ingest.privacy", "ingest",
          "Is `candidate` free of secrets or credentials and of personal data about people other than the operator?",
          version=2, admit=0.5),
    _noul("ingest.usefulness", "ingest",
          "Would `candidate` help a coding agent in a future session with this operator or on this project?",
          version=2, admit=0.5),
    _noul("ingest.scope", "ingest",
          "Is `candidate` about the operator's own stable preferences, working style, tools, environment or "
          "constraints?", version=2, variant=PERSONAL_SCOPE_LABEL, admit=0.5),
    # S4 DEDUP — per candidate pair; supersession asked in two paraphrases.
    QuestionSpec(
        "memory.same_fact", 1, "score", "dedup",
        "Do `memory_a` and `memory_b` state the same fact?",
        levels=(
            (1, "They state different facts."),
            (2, "They might state the same fact, but the wording leaves doubt."),
            (3, "They state the same fact."),
        ),
    ),
    _noul("memory.contradicts", "dedup", "Do `memory_a` and `memory_b` make statements that cannot both be true?",
          propose=0.8),
    _noul("memory.supersedes", "dedup", "Does `memory_b` replace `memory_a` as the current version of the same fact?",
          propose=0.8),
    _noul("memory.supersedes_alt", "dedup", "Is `memory_a` an older version of the fact that `memory_b` states?",
          propose=0.8),
    _noul("memory.same_scope", "dedup", "Are `memory_a` and `memory_b` about the same project or environment?",
          propose=0.8),
    # S5 SKILLS — cookbook shape: gate, choice with abstention, per-candidate fit.
    _noul("skills.needs_procedure", "skills", "Does `request` ask for work that follows a multi-step procedure?",
          gate=0.5),
    QuestionSpec(
        "skills.which_skill", 1, "choice", "skills",
        "Which authorized skill, if any, directly helps with `request`?",
        dynamic_options=True, thresholds={"confidence": 0.7},
    ),
    _noul("skills.fits", "skills", "Does `skill` directly perform the specific work requested in `request`?",
          fit=0.7),
    # S6 HINTS — seven labels over the redacted prompt.
    _noul("hints.decision", "hints", "Does `prompt` state a decision that was made?", show=0.7),
    _noul("hints.constraint", "hints", "Does `prompt` state a rule or limit that must be followed?", show=0.7),
    _noul("hints.bug_root_cause", "hints", "Does `prompt` state the cause of a bug?", show=0.7),
    _noul("hints.environment", "hints",
          "Does `prompt` describe a detail of the machine, tools or configuration being used?", show=0.7),
    _noul("hints.reference", "hints",
          "Does `prompt` point to a document, link or location to consult later?", show=0.7),
    _noul("hints.architecture", "hints", "Does `prompt` describe how the system is structured?", show=0.7),
    _noul("hints.preference", "hints", "Does `prompt` state how the user likes work to be done?", show=0.7),
    # S7 ROUTE.
    QuestionSpec(
        "route.query_type", 1, "choice", "route", "Which type best describes `request`?",
        options=ROUTE_TYPES, thresholds={"confidence": 0.6},
    ),
    # S8 SESSION — per recent confirmed claim in scope.
    QuestionSpec(
        "session.relevant_to_project", 1, "score", "session",
        "How relevant is `memory` to work in `project`?",
        levels=(
            (1, "The memory is unrelated to the project."),
            (2, "The memory is about the same general area as the project."),
            (3, "The memory is useful for work in the project."),
            (4, "The memory is essential for work in the project."),
        ),
    ),
)


class Registry:
    def __init__(self, specs: Iterable[QuestionSpec]) -> None:
        self._by_key: dict[tuple[str, int], QuestionSpec] = {}
        for spec in specs:
            key = (spec.id, spec.version)
            if key in self._by_key:
                raise ValueError(f"duplicate question {spec.key}")
            self._by_key[key] = spec

    def get(self, question_id: str, version: int | None = None, *, variant: str | None = None) -> QuestionSpec:
        """An exact version, else the latest version of the default wording (or of ``variant``)."""
        if version is not None:
            return self._by_key[(question_id, version)]
        versions = [v for (qid, v), spec in self._by_key.items() if qid == question_id and spec.variant == variant]
        if not versions:
            raise KeyError(question_id if variant is None else f"{question_id}:{variant}")
        return self._by_key[(question_id, max(versions))]

    def current(self) -> list[QuestionSpec]:
        """The latest default wording of every question (what builders ask)."""
        ids = dict.fromkeys(spec.id for spec in self._by_key.values() if spec.variant is None)
        return [self.get(question_id) for question_id in ids]

    def all(self) -> list[QuestionSpec]:
        return list(self._by_key.values())


REGISTRY = Registry(_SPECS)


def get(question_id: str, version: int | None = None, *, variant: str | None = None) -> QuestionSpec:
    return REGISTRY.get(question_id, version, variant=variant)


def all_specs() -> list[QuestionSpec]:
    return REGISTRY.all()


def specs_for(surface: str) -> list[QuestionSpec]:
    """The current question of each id on ``surface`` (old versions and variants excluded)."""
    return [spec for spec in REGISTRY.current() if spec.surface == surface]


def default_thresholds(spec: QuestionSpec) -> dict[str, float]:
    return dict(spec.thresholds)


def question_set_id(bound: Iterable[BoundQuestion]) -> str:
    return "+".join(sorted({b.spec.key for b in bound}))


def question_set_sha256(bound: Iterable[BoundQuestion]) -> str:
    hashes = sorted({b.spec.sha256 for b in bound})
    return hashlib.sha256("\n".join(hashes).encode("ascii")).hexdigest()


def primitive_summary(bound: Iterable[BoundQuestion]) -> str:
    counts: dict[str, int] = {}
    for b in bound:
        counts[b.spec.primitive] = counts.get(b.spec.primitive, 0) + 1
    return ",".join(f"{name}:{counts[name]}" for name in sorted(counts))


# ---------------------------------------------------------------- builders ----

def build_revalidate(claim: Mapping[str, Any], *, age_bucket: str = "unknown", access_bucket: str = "unknown",
                     item_ref: str = "") -> tuple[dict[str, Any], list[BoundQuestion]]:
    """S1: one request per stale claim.  Ages are named buckets computed in code."""
    if age_bucket not in AGE_BUCKETS:
        raise ValueError("age_bucket must be a named bucket")
    view = {
        "text": str(claim.get("text") or ""),
        "subject": claim.get("subject"),
        "predicate": claim.get("predicate"),
        "object": claim.get("object_value"),
        "type": claim.get("claim_type"),
        "scope": claim.get("scope"),
        "age": age_bucket,
        "last_used": access_bucket,
    }
    state = {"claim": view}
    return state, [BoundQuestion(spec, item_ref) for spec in specs_for("revalidate")]


def build_recall(query: str, project_label: str,
                 candidates: Sequence[tuple[str, str]]) -> tuple[dict[str, Any], list[BoundQuestion]]:
    """S2: request + project in state; each candidate's text only in its own questions."""
    state = {"request": query, "project": project_label}
    bound: list[BoundQuestion] = []
    for item_ref, text in candidates:
        for question_id in ("recall.relevant", "recall.usable_evidence", "recall.contradicts_request",
                            "recall.instruction_like"):
            bound.append(BoundQuestion(get(question_id), item_ref, {"memory": text}))
    return state, bound


def build_ingest(candidate: str, evidence_quote: str, *, scope_label: str = "", item_ref: str = "",
                 personal: bool | None = None) -> tuple[dict[str, Any], list[BoundQuestion]]:
    """S3: one request per candidate with its own evidence quote.

    A ``personal`` candidate (default: ``scope_label == "personal"``) is asked the
    personal variant of ``ingest.scope`` (ruling R8); every other check is the
    current version for both kinds.
    """
    if personal is None:
        personal = scope_label == PERSONAL_SCOPE_LABEL
    state = {"candidate": candidate, "evidence": evidence_quote, "scope": scope_label}
    bound = []
    for check in INGEST_CHECKS:
        variant = PERSONAL_SCOPE_LABEL if personal and check == "scope" else None
        bound.append(BoundQuestion(get(f"ingest.{check}", variant=variant), item_ref))
    return state, bound


def build_dedup(memory_a: str, memory_b: str, *, scope_label: str = "",
                item_ref: str = "") -> tuple[dict[str, Any], list[BoundQuestion]]:
    """S4: one candidate pair per request (``item_ref`` names the pair, e.g. ``pair:12-34``)."""
    state = {"memory_a": memory_a, "memory_b": memory_b, "scope": scope_label}
    return state, [BoundQuestion(spec, item_ref) for spec in specs_for("dedup")]


def build_skills(query: str, skills: Sequence[tuple[str, str, Mapping[str, Any]]]
                 ) -> tuple[dict[str, Any], list[BoundQuestion]]:
    """S5: ``skills`` is ``(item_ref, option_id, detail)``; ``none`` is always offered."""
    options = (("none", "No authorized skill directly applies to this request."),) + tuple(
        (option_id, json.dumps(dict(detail), ensure_ascii=False, sort_keys=True)) for _, option_id, detail in skills
    )
    bound = [BoundQuestion(get("skills.needs_procedure")), BoundQuestion(get("skills.which_skill"), options=options)]
    bound.extend(BoundQuestion(get("skills.fits"), item_ref, {"skill": dict(detail)}) for item_ref, _, detail in skills)
    return {"request": query}, bound


def build_hints(prompt: str) -> tuple[dict[str, Any], list[BoundQuestion]]:
    return {"prompt": prompt}, [BoundQuestion(get(f"hints.{label}")) for label in HINT_LABELS]


def build_route(query: str) -> tuple[dict[str, Any], list[BoundQuestion]]:
    return {"request": query}, [BoundQuestion(get("route.query_type"))]


def build_session(project_label: str,
                  candidates: Sequence[tuple[str, str]]) -> tuple[dict[str, Any], list[BoundQuestion]]:
    state = {"project": project_label}
    return state, [BoundQuestion(get("session.relevant_to_project"), ref, {"memory": text}) for ref, text in candidates]


__all__ = [
    "AGE_BUCKETS",
    "AnswerSchema",
    "BoundQuestion",
    "HINT_LABELS",
    "INGEST_CHECKS",
    "PERSONAL_SCOPE_LABEL",
    "QuestionSpec",
    "REGISTRY",
    "Registry",
    "all_specs",
    "build_dedup",
    "build_hints",
    "build_ingest",
    "build_recall",
    "build_revalidate",
    "build_route",
    "build_session",
    "build_skills",
    "default_thresholds",
    "get",
    "primitive_summary",
    "question_set_id",
    "question_set_sha256",
    "specs_for",
]
