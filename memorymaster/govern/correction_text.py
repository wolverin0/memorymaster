"""Find the earlier claims a new claim's text explicitly says it corrects (T-2276).

Anchored on an explicit claim reference, never on topic: a human id (``mm-a3f8``) or ``claim 131998``. The
reference must sit in the same clause as a correction cue ("supersedes", "corrige a", "was wrong", "era
incorrecto" ...). Prose that names no id ("the earlier claim about port 8080 was wrong") matches nothing, by design:
guessing the target from similar wording would retire the wrong claim.

Shared by ``govern.ingest_governance`` (the live safety net) and ``scripts/find_prose_corrections.py`` (the
read-only sweep), so the two cannot drift.
"""

from __future__ import annotations

import re
from typing import NamedTuple

MAX_REFERENCES = 5

_HID = r"mm-[0-9a-f]{4}(?:\.\d+)*(?:~\d+)?"
_CLAIM_NUM = r"claim[\s_-]*(?:id)?[\s#:=]*\d{1,12}"
_REF = rf"(?<![\w-])(?P<ref>{_HID}|{_CLAIM_NUM})(?![\w~-]|\.\w)"
_REF_IN_FILLER = re.compile(_HID + "|" + _CLAIM_NUM, re.IGNORECASE)

# A cue verb/noun, then up to four plain words ("the earlier claim", "a la anterior"), then the reference.
_ACTION = (
    r"supersed(?:e|es|ed|ing)|correct(?:s|ed|ing)?|correction\s+(?:of|to|for)|retract(?:s|ed|ing)?|"
    r"replac(?:e|es|ed|ing)|overrid(?:e|es|ing)|invalidat(?:e|es|ed|ing)|obsolet(?:e|es|ed|ing)|"
    r"corrig(?:e|en|iendo)|correcci[oó]n\s+(?:de|a|del)|rectific(?:a|an|ando)|reemplaz(?:a|an|ando)|"
    r"sustituy(?:e|en|endo)|anul(?:a|an|ando)|deja\s+sin\s+efecto|invalida"
)
_CUE_THEN_REF = re.compile(
    rf"(?P<cue>\b(?:{_ACTION}))\b(?P<gap>(?:[\s,:(\"']+[^\W\d_]{{1,14}}){{0,4}})[\s:(\[\"'#]*{_REF}",
    re.IGNORECASE,
)
_REF_THEN_CUE = re.compile(
    rf"{_REF}[\s,)\]\"']*(?:\(?[^\W\d_]{{1,14}}\)?[\s,]+){{0,3}}?(?P<cue>"
    r"(?:was|is|were|are|has\s+been|have\s+been|era|es|fue|ha\s+sido)\s+"
    r"(?:wrong|incorrect|false|inaccurate|invalid|outdated|obsolete|erroneous|superseded|corrected|retracted|replaced|"
    r"incorrect[oa]|fals[oa]|err[oó]ne[oa]|obsolet[oa]|desactualizad[oa]|equivocad[oa]|corregid[oa]|anulad[oa]|"
    r"reemplazad[oa]|sustituid[oa]|invalid[oa])"
    r"|(?:is|are|ya)\s+no\s+longer\s+(?:true|valid|correct|current|accurate)"
    r"|ya\s+no\s+(?:es|est[aá]|vale|aplica|es\s+v[aá]lid[oa]|es\s+correct[oa]))\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(?:not|no|never|nunca|ni|without|sin|cannot|can't|doesn't|don't|didn't|won't)\b", re.IGNORECASE)
_CLAUSE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")
_NEGATION_WINDOW = 24


class Reference(NamedTuple):
    kind: str  # "human_id" | "claim_id"
    value: str | int


def _parse(raw: str) -> Reference:
    token = raw.strip().lower()
    if token.startswith("mm-"):
        return Reference("human_id", token)
    return Reference("claim_id", int(re.search(r"\d+", token).group()))  # type: ignore[union-attr]


def _negated(clause: str, cue_start: int) -> bool:
    return bool(_NEGATION.search(clause[max(0, cue_start - _NEGATION_WINDOW):cue_start]))


def find_corrected_references(text: str) -> list[Reference]:
    """Distinct references the text says it corrects/supersedes, in order of appearance (at most MAX_REFERENCES)."""
    found: list[Reference] = []
    for clause in _CLAUSE_SPLIT.split(text or ""):
        hits: list[tuple[int, str]] = []
        for match in _CUE_THEN_REF.finditer(clause):
            # a second reference inside the filler means the cue belongs to that one, not to this one
            if _REF_IN_FILLER.search(match.group("gap")) or _negated(clause, match.start("cue")):
                continue
            hits.append((match.start("ref"), match.group("ref")))
        for match in _REF_THEN_CUE.finditer(clause):
            if not _negated(clause, match.start("cue")):
                hits.append((match.start("ref"), match.group("ref")))
        for _, raw in sorted(hits):
            reference = _parse(raw)
            if reference not in found:
                found.append(reference)
    return found[:MAX_REFERENCES]
