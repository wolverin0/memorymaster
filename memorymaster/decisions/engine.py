"""Decision orchestration: mode -> key -> breaker -> egress -> budget -> transport -> choose -> explore -> ledger.

``decide`` always returns a :class:`Decision` and never raises.  Any failure
returns the caller's ``legacy_action`` with a logged ``fallback_reason`` (an
``engine_error`` after a request was sent still logs its outcome, tokens and
cost, because the budget and breaker read the ledger):

``mode_off``, ``missing_key``, ``breaker_open`` (while open, only one shadow probe
per surface per ``MEMORYMASTER_JEV_BREAKER_PROBE_S`` is sent; every other call is
logged with no request),
``egress_blocked``, ``budget_exhausted``, ``ledger_unavailable``, a transport
outcome (``timeout``, ``late``, ``http_401``/``403``/``422``/``429``/``529``/
``5xx``/``4xx``, ``redirect_refused``, ``too_large``, ``network_error``,
``malformed``, ``transport_unavailable``, ``request_too_large``),
``choose_error``, ``choose_invalid``, ``invalid_request`` or ``engine_error``.

A late answer never changes an action: when the deadline passes the legacy
action is returned at once and the transport call finishes in the background.
Its outcome is appended as an ``outcomes`` row of kind ``late_answer``, plus the
answers in ``decision_items`` when they still arrived, for measurement only.
With ``HttpTransport`` the socket timeout is the time left before the deadline, so
this is usually a ``timeout`` outcome rather than stored answers.  In ``shadow`` the legacy action is taken and Jev's action is
logged.  In ``off`` nothing is sent, no HTTP client is imported and no row is
written unless ``MEMORYMASTER_DECISIONS_LOG_OFF`` is set.

Never act unlogged: when the decision row cannot be written the returned decision
carries the legacy action and ``ledger_unavailable`` (no Jev action, no answers),
whatever Jev answered.  Nothing is sent after the ledger refused a question
registration.  A hook decision (any ``kind`` but ``batch``) is bounded: its ledger
connections wait at most ``MEMORYMASTER_DECISIONS_HOOK_BUSY_MS`` for another
writer, egress redaction stops (``timeout``, nothing sent) once the deadline has
passed, its request gets only the time left before the deadline (counted from the
start of ``decide``), and the final write only what remains of the deadline plus
``HOOK_SLACK_S``, so ``decide`` returns within the deadline + 100 ms even while
another process holds the ledger's write lock.

Surfaces that fall back before asking log through public APIs:
:meth:`DecisionEngine.record_skip` (one ``skip:<reason>`` row, nothing sent) and
:func:`fallback_record` / :func:`fallback_item_rows` (the rows of a fallback that
sent nothing, for callers that write their own).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from memorymaster.decisions.config import DecisionConfig
from memorymaster.decisions.credentials import ApiKey, get_api_key
from memorymaster.decisions.questions import (
    BoundQuestion,
    primitive_summary,
    question_set_id,
    question_set_sha256,
)

_log = logging.getLogger(__name__)
STATE_SCHEMA_VERSION = 1
LATE_GRACE_S = 0.05
# A hook decision returns within its deadline + HOOK_SLACK_S; the final row write
# keeps FINAL_WRITE_MARGIN_S of that for itself.
HOOK_SLACK_S = 0.1
FINAL_WRITE_MARGIN_S = 0.02
_DEFAULT_EXPLORATION = {"recall": "ranking", "ingest": "binary"}


@dataclass(frozen=True)
class DecisionItem:
    ref: str
    kind: str = "claim"
    rank_legacy: int | None = None


@dataclass
class JevChoice:
    """What the surface's policy would do given Jev's answers.

    Ranking surfaces return ``order`` (all authorized refs, best first), ``k`` and
    ``scores``; the action is ``order[:k]``.  Binary surfaces return ``action``
    and optionally a ``safe_alternative`` for exploration.
    """

    action: Any = None
    order: list[str] | None = None
    k: int | None = None
    scores: dict[str, float] | None = None
    exposed: list[str] | None = None
    safe_alternative: Any = None
    explore_arm: str = "explore_alternative"

    def resolved_action(self, order: Sequence[str] | None = None) -> Any:
        ranked = list(order if order is not None else (self.order or []))
        if self.action is None and self.order is not None:
            return ranked[: self.k] if self.k is not None else ranked
        return self.action


@dataclass(frozen=True)
class DecisionContext:
    kind: str = "hook"  # hook (zero retries, hook deadline) | batch (backoff, batch deadline)
    session_key: str | None = None
    scope: str | None = None
    tenant: str | None = None
    baseline_features: Mapping[str, Any] | None = None
    state_schema_version: int = STATE_SCHEMA_VERSION
    policy_version: str | None = None
    legacy_exposed: Sequence[str] | None = None
    delivery_filter: Callable[[list[str]], list[str]] | None = None
    exploration: str | None = None  # "ranking" | "binary" | "none"; default per surface


@dataclass(frozen=True)
class Decision:
    decision_id: str
    action: Any
    items: list[str]
    mode: str
    fallback_reason: str | None
    jev_action: Any = None
    exploration_arm: str | None = None
    answers: Any = None
    logged: bool = False


class Answers:
    """Read access to validated answers by ``(question_id, item_ref)``."""

    def __init__(self, parsed: Mapping[str, Any], bound: Sequence[BoundQuestion],
                 thresholds: Mapping[str, Mapping[str, float]]) -> None:
        self._by: dict[tuple[str, str], Any] = {}
        self._keys: dict[str, str] = {}
        for question in bound:
            if question.wire_id in parsed:
                self._by[(question.spec.id, question.item_ref)] = parsed[question.wire_id]
            self._keys[question.spec.id] = question.spec.key
        self.thresholds = {key: dict(value) for key, value in thresholds.items()}

    def get(self, question_id: str, item_ref: str = "") -> Any:
        return self._by.get((question_id, item_ref))

    def _value(self, question_id: str, item_ref: str, primitive: str) -> Any:
        answer = self.get(question_id, item_ref)
        return answer.value if answer is not None and answer.primitive == primitive else None

    def noul(self, question_id: str, item_ref: str = "") -> float | None:
        return self._value(question_id, item_ref, "noul")

    def score(self, question_id: str, item_ref: str = "") -> float | None:
        return self._value(question_id, item_ref, "score")

    def choice(self, question_id: str, item_ref: str = "") -> str | None:
        return self._value(question_id, item_ref, "choice")

    def threshold(self, question_id: str, name: str, default: float | None = None) -> float | None:
        key = self._keys.get(question_id)
        return self.thresholds.get(key, {}).get(name, default) if key else default

    def item_refs(self) -> list[str]:
        seen: list[str] = []
        for _, ref in self._by:
            if ref and ref not in seen:
                seen.append(ref)
        return seen


def _dumps(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate (a "\ud800" JSON escape): keep it escaped so the row is written
        text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return text


def code_revision() -> str:
    """The MemoryMaster version recorded as ``code_revision`` in every row."""
    try:
        import memorymaster

        return str(getattr(memorymaster, "__version__", "unknown"))
    except Exception:
        return "unknown"


_code_revision = code_revision


def fallback_record(decision_id: str, surface: str, *, mode: str, reason: str, legacy_action: Any,
                    baseline_features: Mapping[str, Any] | None = None, session_key: str | None = None,
                    scope: str | None = None, tenant: str | None = None, engine_ms: int | None = None,
                    policy_version: str | None = None) -> Any:
    """The decision row of a fallback that sent nothing (a ledger ``DecisionRecord``).

    ``not_sent``, ``attempt_count`` 0 (so neither the breaker nor the RPM budget
    counts it), no tokens or cost, the legacy action taken with propensity 1 and
    exploration arm ``fallback``; no request or state text.
    """
    from memorymaster.decisions import policy as pol
    from memorymaster.decisions.ledger import DecisionRecord

    return DecisionRecord(
        decision_id=decision_id, surface=surface, mode=mode, fallback_reason=reason,
        policy_version=policy_version or pol.POLICY_VERSION, backend="typesafe", code_revision=code_revision(),
        state_schema_version=STATE_SCHEMA_VERSION, transport_outcome="not_sent", attempt_count=0,
        tokens_in=0, tokens_out=0, cost_usd=0.0, engine_ms=engine_ms,
        legacy_action=_dumps(legacy_action), action_taken=_dumps(legacy_action), exploration_arm="fallback",
        available_actions_json=_dumps([legacy_action]),
        action_propensities_json=_dumps([{"action": legacy_action, "p": 1.0}]), chosen_propensity=1.0,
        randomization_id=pol.randomization_id(decision_id, surface),
        baseline_features_json=_dumps(baseline_features) if baseline_features is not None else None,
        session_key=session_key, scope=scope, tenant=tenant,
    )


def fallback_item_rows(decision_id: str, refs: Iterable[str], *, kind: str = "claim",
                       delivered: Iterable[str] | None = None) -> list[Any]:
    """Item rows for the legacy selection of a fallback: each ref exposed at its legacy rank.

    ``delivered`` names the refs that reached the reader (default: all of them).
    """
    from memorymaster.decisions.ledger import ItemRecord

    ordered = list(dict.fromkeys(refs))
    reached = set(ordered if delivered is None else delivered)
    return [ItemRecord(decision_id=decision_id, item_ref=ref, item_kind=kind, rank_legacy=rank, rank_final=rank,
                       exposed=1, delivered=int(ref in reached))
            for rank, ref in enumerate(ordered, start=1)]


def _refs(value: Any) -> list[str] | None:
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


@dataclass
class _Pending:
    """Lets ``decide`` log an ``engine_error`` once a decision row can be written.

    ``_decide`` fills it as soon as ``finish`` exists and keeps ``mode`` current, so
    an unexpected failure after a paid request still records its transport outcome,
    tokens and cost (the budget, RPM cap and breaker all read the ledger).
    """

    finish: Callable[..., Decision] | None = None
    mode: str = "off"
    started: float = 0.0  # engine clock at the start of decide(), for ``engine_ms``
    hard_end: float | None = None  # hook decisions: engine clock by which decide() returns


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class _Late:
    """Hands a transport call to a daemon thread and reports whether it beat the deadline."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.result: Any = None
        self.finished = False
        self.late = False
        self.on_late: Callable[[Any], None] | None = None


class DecisionEngine:
    def __init__(
        self,
        config: DecisionConfig | None = None,
        *,
        ledger: Any = None,
        transport_factory: Callable[[ApiKey], Any] | None = None,
        key_lookup: Callable[[], ApiKey | None] | None = None,
        id_factory: Callable[[], str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or DecisionConfig.from_env()
        self._ledger = ledger
        self._transport_factory = transport_factory
        self._key_lookup = key_lookup or get_api_key
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._monotonic = monotonic
        self._budget: Any = None
        self._transport: Any = None
        self._transport_key: ApiKey | None = None
        self._late_threads: list[threading.Thread] = []
        self._registered: set[str] = set()

    # ------------------------------------------------------------- plumbing ---
    @property
    def ledger(self) -> Any:
        if self._ledger is None:
            from memorymaster.decisions.ledger import DecisionLedger

            self._ledger = DecisionLedger(self.config.decisions_db)
        return self._ledger

    def _get_budget(self) -> Any:
        if self._budget is None:
            from memorymaster.decisions.policy import Budget

            self._budget = Budget(self.ledger, self.config)
        return self._budget

    def _get_transport(self, key: ApiKey) -> Any:
        if self._transport is None or self._transport_key is None or self._transport_key.reveal() != key.reveal():
            if self._transport_factory is not None:
                self._transport = self._transport_factory(key)
            else:
                from memorymaster.decisions.transport import transport_class

                self._transport = transport_class()(key)
            self._transport_key = key
        return self._transport

    def record_skip(self, surface: str, *, reason: str, legacy_action: Any, session_key: str | None = None,
                    state: Mapping[str, Any] | None = None, items: Sequence[DecisionItem | str] = ()) -> bool:
        """Log that ``surface`` skipped Jev before asking: one ``skip:<reason>`` row; nothing is sent.

        The row is a :func:`fallback_record` in the surface's current mode, with the
        legacy action taken and item rows for ``items`` (exposed when the legacy
        action names them).  ``state`` is stored only after egress redaction, and
        withheld when it cannot be redacted.  Like :meth:`decide`, ``off`` writes
        nothing unless ``MEMORYMASTER_DECISIONS_LOG_OFF`` is set.  The write waits at
        most ``MEMORYMASTER_DECISIONS_HOOK_BUSY_MS`` for another writer.  Returns
        whether the row was written; never raises.
        """
        try:
            from memorymaster.decisions.config import SURFACES

            name = surface.strip().lower() if isinstance(surface, str) else ""
            if name not in SURFACES or not isinstance(reason, str) or not reason.strip():
                return False
            mode = self.config.mode_for(name)
            if mode == "off" and not self.config.log_off:
                return False
            decision_id = self._safe_id()
            normalized = [item if isinstance(item, DecisionItem) else DecisionItem(str(item)) for item in items]
            known = {item.ref for item in normalized}
            legacy_exposed = [ref for ref in (_refs(legacy_action) or []) if ref in known]
            record = fallback_record(decision_id, name, mode=mode, reason=f"skip:{reason.strip()}",
                                     legacy_action=legacy_action, session_key=session_key, engine_ms=0)
            if state is not None:
                from memorymaster.decisions.egress import prepare_egress_value

                redacted = prepare_egress_value(state)
                record.redaction_counts_json = _dumps(redacted.counts)
                if not redacted.blocked:
                    record.state_redacted = _dumps({"state": redacted.value, "subjects": {}})
                    record.state_sha256 = hashlib.sha256(record.state_redacted.encode("utf-8")).hexdigest()
            rows = self._item_rows(decision_id, normalized, [], None, legacy_exposed, legacy_exposed, legacy_exposed)
            bounded = getattr(self.ledger, "bounded", None)
            with bounded(self.config.hook_busy_ms) if callable(bounded) else contextlib.nullcontext():
                return bool(self.ledger.write_decision(record, rows))
        except Exception as exc:  # telemetry must never break the surface
            _log.debug("skip row not written (%s)", type(exc).__name__)
            return False

    def wait_for_late_answers(self, timeout: float = 10.0) -> None:
        """Test/shutdown helper: wait for daemon threads still holding late answers."""
        deadline = time.monotonic() + timeout
        for thread in list(self._late_threads):
            thread.join(max(0.0, deadline - time.monotonic()))

    # --------------------------------------------------------------- decide ---
    def decide(
        self,
        surface: str,
        *,
        state: Mapping[str, Any],
        questions: Sequence[BoundQuestion],
        items: Sequence[DecisionItem | str],
        legacy_action: Any,
        choose: Callable[[Answers], JevChoice],
        context: DecisionContext | Mapping[str, Any] | None = None,
    ) -> Decision:
        surface = str(surface).strip().lower()
        try:
            ctx = context if isinstance(context, DecisionContext) else DecisionContext(**dict(context or {}))
        except TypeError:
            ctx = DecisionContext()
        decision_id = self._safe_id()
        known = {item.ref if isinstance(item, DecisionItem) else str(item) for item in items}
        if ctx.legacy_exposed is not None:
            legacy_exposed = list(ctx.legacy_exposed)
        else:  # a legacy action counts as exposure only when it names the decision's items
            legacy_exposed = [ref for ref in (_refs(legacy_action) or []) if ref in known]
        pending = _Pending(started=self._monotonic())
        with contextlib.ExitStack() as scope:
            try:
                if ctx.kind != "batch":
                    budget_s = self.config.deadline_ms(ctx.kind) / 1000.0 + HOOK_SLACK_S
                    pending.hard_end = pending.started + budget_s
                    self._bound_ledger(scope, surface, time.perf_counter() + budget_s - FINAL_WRITE_MARGIN_S)
                return self._decide(surface, decision_id, state, list(questions), items, legacy_action, choose, ctx,
                                    legacy_exposed, pending)
            except Exception as exc:  # the caller must always get its deterministic action back
                _log.debug("decision engine error (%s)", type(exc).__name__)
                if pending.finish is not None:
                    try:  # without answers: they may be what failed
                        return pending.finish("engine_error", effective_mode=pending.mode)
                    except Exception as again:
                        _log.debug("engine_error row not written (%s)", type(again).__name__)
                return Decision(decision_id, legacy_action, legacy_exposed, self.config.mode_for(surface),
                                "engine_error")

    def _bound_ledger(self, scope: contextlib.ExitStack, surface: str, until: float) -> None:
        """Hook decisions: each ledger write of this thread waits at most ``hook_busy_ms`` for
        another writer, and never past ``until`` (``perf_counter``), which leaves the final
        row write ``FINAL_WRITE_MARGIN_S`` inside the deadline + ``HOOK_SLACK_S``."""
        if self.config.mode_for(surface) == "off" and not self.config.log_off:
            return  # off never opens the ledger
        try:
            bounded = getattr(self.ledger, "bounded", None)
            if callable(bounded):
                scope.enter_context(bounded(self.config.hook_busy_ms, until=until))
        except Exception as exc:
            _log.debug("hook ledger bound not applied (%s)", type(exc).__name__)

    def _engine_ms(self, started: float) -> int:
        return max(0, int(round((self._monotonic() - started) * 1000)))

    def _safe_id(self) -> str:
        try:
            value = str(self._id_factory())
        except Exception:
            value = ""
        return value or uuid.uuid4().hex

    def _decide(self, surface: str, decision_id: str, state: Mapping[str, Any], bound: list[BoundQuestion],
                raw_items: Sequence[DecisionItem | str], legacy_action: Any, choose: Callable[[Answers], JevChoice],
                ctx: DecisionContext, legacy_exposed: list[str], pending: _Pending) -> Decision:
        mode = self.config.mode_for(surface)
        items = [item if isinstance(item, DecisionItem) else DecisionItem(str(item)) for item in raw_items]
        if mode == "off":
            logged = False
            if self.config.log_off:
                logged = self._write_minimal(surface, decision_id, legacy_action, legacy_exposed, items, ctx,
                                             pending.started)
            return Decision(decision_id, legacy_action, legacy_exposed, "off", "mode_off", logged=logged)

        from memorymaster.decisions import policy as pol
        from memorymaster.decisions.egress import prepare_egress_value
        from memorymaster.decisions.ledger import DecisionRecord

        record = DecisionRecord(
            decision_id=decision_id,
            surface=surface,
            mode=mode,
            policy_version=ctx.policy_version or pol.POLICY_VERSION,
            question_set_id=question_set_id(bound),
            question_sha256=question_set_sha256(bound),
            primitive_summary=primitive_summary(bound),
            backend="typesafe",
            code_revision=_code_revision(),
            state_schema_version=ctx.state_schema_version,
            legacy_action=_dumps(legacy_action),
            baseline_features_json=_dumps(ctx.baseline_features) if ctx.baseline_features is not None else None,
            randomization_id=pol.randomization_id(decision_id, surface),
            session_key=ctx.session_key,
            scope=ctx.scope,
            tenant=ctx.tenant,
            transport_outcome="not_sent",
            attempt_count=0,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
        )

        def finish(reason: str | None, *, effective_mode: str, answers: Any = None, jev_action: Any = None,
                   exploration: Any = None, final_order: list[str] | None = None,
                   action: Any = legacy_action, exposed: list[str] | None = None) -> Decision:
            exposed_refs = list(exposed if exposed is not None else legacy_exposed)
            delivered = self._delivered(ctx, exposed_refs)
            record.mode = effective_mode
            record.fallback_reason = reason
            record.engine_ms = self._engine_ms(pending.started)
            record.action_taken = _dumps(action)
            record.jev_action = _dumps(jev_action) if jev_action is not None else None
            if exploration is not None:
                record.available_actions_json = _dumps(exploration.available_actions)
                record.action_propensities_json = exploration.propensities_json()
                record.chosen_propensity = exploration.chosen_propensity
                record.exploration_arm = exploration.arm
            else:
                record.available_actions_json = _dumps([action])
                record.action_propensities_json = _dumps([{"action": action, "p": 1.0}])
                record.chosen_propensity = 1.0
                # A breaker row that sent nothing observed no Jev answer: a fallback, not shadow.
                sent = bool(record.attempt_count)
                record.exploration_arm = "fallback" if reason and (reason != "breaker_open" or not sent) else "shadow"
            known_refs = {item.ref for item in items}
            order = final_order if final_order is not None else (
                [ref for ref in (_refs(action) or []) if ref in known_refs] or legacy_exposed)
            rows = self._item_rows(decision_id, items, bound, answers, order, exposed_refs, delivered)
            if not self.ledger.write_decision(record, rows):
                # Never act on a decision whose row was not written: the legacy action, marked.
                legacy_delivered = delivered if exposed_refs == legacy_exposed else self._delivered(ctx, legacy_exposed)
                return Decision(decision_id, legacy_action, legacy_delivered, effective_mode, "ledger_unavailable",
                                None, "fallback", None, False)
            arm = record.exploration_arm
            return Decision(decision_id, action, delivered, effective_mode, reason, jev_action, arm,
                            answers, True)

        pending.finish, pending.mode = finish, mode
        # Request validity first: a caller bug must not reach the network.
        wire_ids = [q.wire_id for q in bound]
        if not wire_ids or len(set(wire_ids)) != len(wire_ids):
            return finish("invalid_request", effective_mode=mode)
        try:
            _dumps_strict({"state": state, "subjects": [q.subject for q in bound]})
        except (TypeError, ValueError):
            return finish("invalid_request", effective_mode=mode)

        key = self._key_lookup()
        if key is None:
            return finish("missing_key", effective_mode=mode)

        breaker = pol.Breaker(self.ledger)
        breaker_state = breaker.state(surface)
        if breaker_state == "unknown":
            # The breaker could not be read (e.g. another hook process held a lock past the
            # bound): an unreadable ledger fails closed, so nothing is sent.
            return finish("ledger_unavailable", effective_mode=mode)
        breaker_open = breaker_state == "open"
        effective_mode = "shadow" if breaker_open else mode
        pending.mode = effective_mode
        if breaker_open and not breaker.claim_probe(surface, self.config.breaker_probe_s):
            # Open breaker: only one shadow probe per interval (decided in the ledger, so every
            # hook process agrees); this call sends nothing and costs no deadline.
            return finish("breaker_open", effective_mode=effective_mode)

        # Egress is CPU work before the send: each distinct value is redacted once (a recall
        # candidate is the subject of four questions), and a hook decision stops between
        # values once its deadline has passed (sends nothing; the row says ``timeout``).
        hook_end = (pending.started + self.config.deadline_ms(ctx.kind) / 1000.0
                    if pending.hard_end is not None else None)
        redacted_once: dict[str, Any] = {}

        def egress(value: Any) -> Any:
            key = _dumps(value)
            if key not in redacted_once:
                redacted_once[key] = prepare_egress_value(value)
            return redacted_once[key]

        egress_state = egress(state)
        egress_subjects: list[Any] = []
        egress_options: list[Any] = []
        for q in bound:
            if hook_end is not None and self._monotonic() >= hook_end:
                return finish("timeout", effective_mode=effective_mode)
            egress_subjects.append(egress(q.subject) if q.subject else None)
            # Dynamic choice options: option ids are code-defined; only descriptions are redacted.
            egress_options.append(
                egress([description for _, description in q.effective_options()])
                if q.spec.primitive == "choice" and q.options is not None else None
            )
        counts: dict[str, int] = dict(egress_state.counts)
        blocked = egress_state.blocked
        for part in [*egress_subjects, *egress_options]:
            if part is None:
                continue
            for name, n in part.counts.items():
                counts[name] = counts.get(name, 0) + n
            blocked = blocked or part.blocked
        record.redaction_counts_json = _dumps(counts)
        if blocked:
            return finish("egress_blocked", effective_mode=effective_mode)

        from memorymaster.decisions.transport import (
            MAX_BATCH_RETRIES,
            MODEL,
            TRANSPORT_VERSION,
            cost_usd,
            estimate_tokens,
            transport_version,
        )

        questions_wire: dict[str, Any] = {}
        subjects_log: dict[str, Any] = {}
        for question, subject, options in zip(bound, egress_subjects, egress_options):
            redacted_question = question
            if options is not None:
                ids = [option_id for option_id, _ in question.effective_options()]
                redacted_question = BoundQuestion(question.spec, question.item_ref, question.subject,
                                                  tuple(zip(ids, options.value)))
            questions_wire[question.wire_id] = redacted_question.wire(subject.value if subject else None)
            if subject is not None and question.item_ref:
                subjects_log.setdefault(question.item_ref, subject.value)
        payload = {"model": MODEL, "state": egress_state.value, "questions": questions_wire}
        logged_state = {"state": egress_state.value, "subjects": subjects_log}
        record.state_redacted = _dumps(logged_state)
        record.state_sha256 = hashlib.sha256(record.state_redacted.encode("utf-8")).hexdigest()
        record.egress_bytes = len(_dumps(payload).encode("utf-8"))
        record.model_requested = MODEL
        record.transport_version = TRANSPORT_VERSION
        record.sdk_version = transport_version()

        new_specs = {q.spec.key for q in bound} - self._registered
        if new_specs:
            if not self.ledger.register_questions({q.spec.key: q.spec for q in bound}.values()):
                # The ledger refused a write (e.g. another process holds its lock): spend nothing
                # it may be unable to record.
                return finish("ledger_unavailable", effective_mode=effective_mode)
            self._registered |= new_specs
        thresholds = pol.resolve_thresholds(self.ledger, bound, register=False)
        if thresholds is None:  # the stored thresholds could not be read: send nothing
            return finish("ledger_unavailable", effective_mode=effective_mode)
        record.thresholds_json = _dumps(thresholds)

        budget_reason = self._get_budget().check(estimated_cost=cost_usd(estimate_tokens(record.egress_bytes), 0))
        if budget_reason is not None:
            return finish(budget_reason, effective_mode=effective_mode)

        expected = {q.wire_id: q.answer_schema() for q in bound}
        deadline_s = self.config.deadline_ms(ctx.kind) / 1000.0
        if pending.hard_end is not None:  # a hook's deadline runs from the start of decide()
            deadline_s = pending.started + deadline_s - self._monotonic()
            if deadline_s <= 0:
                return finish("timeout", effective_mode=effective_mode)
        retries = MAX_BATCH_RETRIES if ctx.kind == "batch" else 0
        # Last gate before the network: a durable send intent. Every earlier step may be a
        # pure read, so a write-locked or read-only ledger would otherwise let a request
        # (and its cost) leave with no row the caps, breaker or operator could ever see.
        if not self.ledger.reserve_send(decision_id, surface, ts=record.ts,
                                        est_cost_usd=cost_usd(estimate_tokens(record.egress_bytes), 0)):
            return finish("ledger_unavailable", effective_mode=effective_mode)
        transport = self._get_transport(key)
        sent_at = self._monotonic()
        result, timed_out = self._call_with_deadline(
            lambda: transport.send(payload, expected=expected, timeout_s=deadline_s, max_retries=retries),
            deadline_s,
            lambda late: self._record_late(decision_id, items, bound, late, legacy_exposed),
        )
        if timed_out:
            record.transport_outcome = "timeout"
            record.attempt_count = 1
            record.latency_ms = int(round(deadline_s * 1000))
            est = estimate_tokens(record.egress_bytes)
            record.tokens_in, record.cost_usd = est, cost_usd(est, 0)
            return finish("breaker_open" if breaker_open else "timeout", effective_mode=effective_mode)

        elapsed = self._monotonic() - sent_at
        record.latency_ms = getattr(result, "latency_ms", None)
        record.attempt_count = getattr(result, "attempt_count", 1)
        record.model_served = getattr(result, "model_served", None)
        record.tokens_in = getattr(result, "tokens_in", 0)
        record.tokens_out = getattr(result, "tokens_out", 0)
        record.cost_usd = getattr(result, "cost_usd", 0.0)
        outcome = getattr(result, "outcome", "malformed")
        if outcome == "ok" and elapsed > deadline_s:
            record.transport_outcome = "late"
            return finish("breaker_open" if breaker_open else "late", effective_mode=effective_mode,
                          answers=result.answers)
        record.transport_outcome = outcome
        if outcome != "ok" or not result.answers:
            return finish("breaker_open" if breaker_open else outcome, effective_mode=effective_mode)

        answers = Answers(result.answers, bound, thresholds)
        try:
            choice = choose(answers)
        except Exception:
            return finish("breaker_open" if breaker_open else "choose_error", effective_mode=effective_mode,
                          answers=result.answers)
        authorized = {item.ref for item in items}
        if not isinstance(choice, JevChoice) or not self._choice_is_authorized(choice, authorized):
            return finish("breaker_open" if breaker_open else "choose_invalid", effective_mode=effective_mode,
                          answers=result.answers)
        jev_action = choice.resolved_action()
        if effective_mode != "live":
            return finish("breaker_open" if breaker_open else None, effective_mode=effective_mode,
                          answers=result.answers, jev_action=jev_action)

        exploration, final_order = self._explore(surface, decision_id, choice, ctx)
        action = choice.resolved_action(final_order) if final_order is not None else exploration.action
        if final_order is None and _refs(action) is not None:
            final_order = _refs(action)
        exposed = choice.exposed if choice.exposed is not None and exploration.arm == "policy" else _refs(action)
        exposed = [ref for ref in (exposed or []) if ref in authorized]
        return finish(None, effective_mode="live", answers=result.answers, jev_action=jev_action,
                      exploration=exploration, final_order=final_order, action=action, exposed=exposed or [])

    # ------------------------------------------------------------- helpers ---
    def _explore(self, surface: str, decision_id: str, choice: JevChoice, ctx: DecisionContext):
        from memorymaster.decisions import policy as pol

        rate = self.config.explore_rate(surface)
        kind = ctx.exploration or _DEFAULT_EXPLORATION.get(surface, "binary")
        if kind == "ranking" and choice.order is not None:
            result = pol.explore_ranking(decision_id, surface, policy_order=choice.order,
                                         scores=choice.scores or {}, rate=rate)
            return result, list(result.action)
        alternative = choice.safe_alternative if kind == "binary" else None
        result = pol.explore_binary(decision_id, surface, policy_action=choice.resolved_action(),
                                    safe_alternative=alternative, rate=rate, arm_name=choice.explore_arm)
        return result, list(choice.order) if choice.order is not None and result.arm == "policy" else None

    @staticmethod
    def _choice_is_authorized(choice: JevChoice, authorized: set[str]) -> bool:
        """``order``/``exposed`` must only name items code authorized; ``action`` is surface-opaque.

        ``scores`` feed the Plackett-Luce exploration, so every value must be a finite
        number and every ref in ``order`` must have one (``Answers.score`` returns
        ``None`` for a missing answer or the wrong primitive).
        """
        for refs in (choice.order, choice.exposed):
            if refs is not None and not (isinstance(refs, (list, tuple)) and all(isinstance(r, str) for r in refs)
                                         and set(refs) <= authorized):
                return False
        if choice.order is not None and len(set(choice.order)) != len(choice.order):
            return False
        if choice.k is not None and (not isinstance(choice.k, int) or choice.k < 0):
            return False
        if choice.scores is not None:
            if not isinstance(choice.scores, Mapping) or not all(_finite_number(v) for v in choice.scores.values()):
                return False
            if choice.order is not None and not all(ref in choice.scores for ref in choice.order):
                return False
        return True

    @staticmethod
    def _delivered(ctx: DecisionContext, exposed: list[str]) -> list[str]:
        if ctx.delivery_filter is None:
            return list(exposed)
        try:
            delivered = list(ctx.delivery_filter(list(exposed)))
        except Exception:
            return list(exposed)
        return [ref for ref in delivered if ref in exposed]

    def _call_with_deadline(self, call: Callable[[], Any], deadline_s: float,
                            on_late: Callable[[Any], None]) -> tuple[Any, bool]:
        box = _Late()
        box.on_late = on_late

        def runner() -> None:
            try:
                value = call()
            except Exception as exc:
                from memorymaster.decisions.transport import TransportResult

                value = TransportResult(None, None, 0, 1, "network_error", error_class=type(exc).__name__)
            with box.lock:
                box.result = value
                box.finished = True
                late = box.late
            box.done.set()
            if late and box.on_late is not None:
                try:
                    box.on_late(value)
                except Exception:
                    pass

        thread = threading.Thread(target=runner, name="jev-decision", daemon=True)
        thread.start()
        box.done.wait(deadline_s + LATE_GRACE_S)
        with box.lock:
            if box.finished:
                return box.result, False
            box.late = True
        self._late_threads = [t for t in self._late_threads if t.is_alive()] + [thread]
        return None, True

    def _record_late(self, decision_id: str, items: Sequence[DecisionItem], bound: Sequence[BoundQuestion],
                     result: Any, legacy_exposed: list[str]) -> None:
        from memorymaster.decisions.ledger import OutcomeRecord

        answers = getattr(result, "answers", None) if getattr(result, "outcome", None) == "ok" else None
        if answers:
            rows = self._item_rows(decision_id, items, bound, answers, legacy_exposed, legacy_exposed,
                                   legacy_exposed, answers_only=True)
            self.ledger.write_items(rows)
        details = {"transport_outcome": getattr(result, "outcome", None),
                   "model_served": getattr(result, "model_served", None),
                   "latency_ms": getattr(result, "latency_ms", None)}
        self.ledger.record_outcomes([OutcomeRecord(
            decision_id, "", "late_answer", float(getattr(result, "latency_ms", 0) or 0),
            reward_version="late_answer.v1", label_source="transport", details_json=_dumps(details),
        )])

    @staticmethod
    def _item_rows(decision_id: str, items: Sequence[DecisionItem], bound: Sequence[BoundQuestion], answers: Any,
                   order: Sequence[str], exposed: Sequence[str], delivered: Sequence[str],
                   answers_only: bool = False) -> list[Any]:
        from memorymaster.decisions.ledger import ItemRecord

        kinds = {item.ref: item.kind for item in items}
        legacy_ranks = {item.ref: item.rank_legacy for item in items}
        final_ranks = {ref: index + 1 for index, ref in enumerate(order)}
        exposed_set, delivered_set = set(exposed), set(delivered)
        rows: list[Any] = []
        answered_refs: set[str] = set()

        def base(ref: str) -> dict[str, Any]:
            return dict(decision_id=decision_id, item_ref=ref, item_kind=kinds.get(ref),
                        rank_legacy=legacy_ranks.get(ref), rank_final=final_ranks.get(ref),
                        exposed=int(ref in exposed_set), delivered=int(ref in delivered_set))

        if answers:
            for question in bound:
                parsed = answers.get(question.wire_id)
                if parsed is None:
                    continue
                answered_refs.add(question.item_ref)
                rows.append(ItemRecord(
                    question_id=question.spec.id, question_version=question.spec.version,
                    answer=str(parsed.value), probabilities_json=_dumps(parsed.probabilities),
                    confidence=parsed.confidence, **base(question.item_ref),
                ))
        if not answers_only:
            refs: list[str] = []
            for ref in [*(item.ref for item in items), *exposed, *order]:
                if ref not in refs:
                    refs.append(ref)
            for ref in refs:
                if ref not in answered_refs:
                    rows.append(ItemRecord(question_id="", **base(ref)))
        return rows

    def _write_minimal(self, surface: str, decision_id: str, legacy_action: Any, legacy_exposed: list[str],
                       items: Sequence[DecisionItem], ctx: DecisionContext, started: float) -> bool:
        from memorymaster.decisions.ledger import DecisionRecord

        record = DecisionRecord(
            decision_id=decision_id, surface=surface, mode="off", fallback_reason="mode_off",
            engine_ms=self._engine_ms(started),
            transport_outcome="not_sent", attempt_count=0, cost_usd=0.0, legacy_action=_dumps(legacy_action),
            action_taken=_dumps(legacy_action), session_key=ctx.session_key, scope=ctx.scope, tenant=ctx.tenant,
            state_schema_version=ctx.state_schema_version, code_revision=_code_revision(),
        )
        delivered = self._delivered(ctx, legacy_exposed)
        rows = self._item_rows(decision_id, items, [], None, legacy_exposed, legacy_exposed, delivered)
        return self.ledger.write_decision(record, rows)


def _dumps_strict(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


_default_engine: DecisionEngine | None = None
_default_lock = threading.Lock()


def default_engine() -> DecisionEngine:
    """Process-wide engine rebuilt when the environment configuration changes."""
    global _default_engine
    config = DecisionConfig.from_env()
    with _default_lock:
        if _default_engine is None or _default_engine.config != config:
            _default_engine = DecisionEngine(config)
        return _default_engine


def decide(surface: str, *, state: Mapping[str, Any], questions: Iterable[BoundQuestion],
           items: Sequence[DecisionItem | str], legacy_action: Any, choose: Callable[[Answers], JevChoice],
           context: DecisionContext | Mapping[str, Any] | None = None,
           engine: DecisionEngine | None = None) -> Decision:
    """Module-level entry point used by surfaces (see the module docstring)."""
    return (engine or default_engine()).decide(surface, state=state, questions=list(questions), items=items,
                                               legacy_action=legacy_action, choose=choose, context=context)


__all__ = [
    "Answers",
    "Decision",
    "DecisionContext",
    "DecisionEngine",
    "DecisionItem",
    "JevChoice",
    "code_revision",
    "decide",
    "default_engine",
    "fallback_item_rows",
    "fallback_record",
]
