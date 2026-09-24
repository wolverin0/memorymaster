"""Offline Jev fakes shared by the 4.9.0 surface tests (no network, no real key)."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from memorymaster.decisions.config import DecisionConfig
from memorymaster.decisions.credentials import ApiKey
from memorymaster.decisions.engine import DecisionEngine
from memorymaster.decisions.transport import ParsedAnswer, TransportResult

KEY = ApiKey("ts-test-" + "G" * 24)  # synthetic
TOKENS_IN = 1000
COST = TOKENS_IN * 0.042e-6


class ScriptedTransport:
    """Answers every question from a table: ``values[question_id]``, overridden per item ref.

    A noul value is a float; a score value is ``{level: probability}``.  ``outcome``
    other than ``ok`` fails every request; ``on_send`` runs before answering.
    """

    def __init__(self, values: Mapping[str, Any] | None = None, *,
                 per_ref: Mapping[str, Mapping[str, Any]] | None = None, outcome: str = "ok",
                 on_send: Callable[[dict], None] | None = None) -> None:
        self.values = dict(values or {})
        self.per_ref = {ref: dict(v) for ref, v in (per_ref or {}).items()}
        self.outcome = outcome
        self.on_send = on_send
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def send(self, payload, *, expected, timeout_s, max_retries=0):
        with self._lock:
            self.calls.append(payload)
        if self.on_send is not None:
            self.on_send(payload)
        if self.outcome != "ok":
            return TransportResult(None, None, 5, 1, self.outcome)
        answers = {}
        for wire_id, schema in expected.items():
            question_id, _, ref = wire_id.partition("::")
            table = {**self.values, **self.per_ref.get(ref, {})}
            value = table.get(question_id, 0.5)
            if schema.primitive == "noul":
                answers[wire_id] = ParsedAnswer("noul", float(value), {"noul": float(value)})
            elif schema.primitive == "score":
                levels = [str(level) for level in schema.levels]
                probs = dict(value) if isinstance(value, Mapping) else {level: 1.0 / len(levels) for level in levels}
                label = max(probs, key=probs.get)
                answers[wire_id] = ParsedAnswer("score", 0.5, probs, None, label)
            else:
                option = schema.options[0]
                answers[wire_id] = ParsedAnswer("choice", option, {o: float(o == option) for o in schema.options},
                                                0.9, option)
        return TransportResult(200, {"model": "jev-1.13.0"}, 5, 1, "ok", model_served="jev-1.13.0",
                               tokens_in=TOKENS_IN, tokens_out=3, cost_usd=COST, answers=answers)

    def close(self) -> None:
        pass

    def sent_text(self) -> str:
        return json.dumps(self.calls, ensure_ascii=False)


def make_engine(ledger: Path, transport: ScriptedTransport, *, mode: str = "live",
                env: Mapping[str, str] | None = None) -> DecisionEngine:
    environ = {"MEMORYMASTER_JEV_MODE": mode, "MEMORYMASTER_DECISIONS_DB": str(ledger)}
    environ.update(env or {})
    return DecisionEngine(DecisionConfig.from_env(environ), transport_factory=lambda _key: transport,
                          key_lookup=lambda: KEY)
