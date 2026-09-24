#!/usr/bin/env python3
"""Classify user messages and inject MemoryMaster routing hints.

Mirrors the obsidian-mind classify-message.py pattern but adapted for the
coding-agent use case: instead of routing to vault folders, routes to
MemoryMaster claim_types (decision, gotcha, constraint, architecture, bug,
environment, reference).

Data-driven regex signal matching with Latin-letter lookarounds
(?<![a-zA-Z]) instead of \\b — safe for mixed Spanish/English technical
text. Zero LLM calls, zero external deps, ~5ms runtime.

Output: hookSpecificOutput.additionalContext with routing hints.
Claude reads them and decides whether to call ingest_claim.

Jev (surface ``hints``, S6): when MEMORYMASTER_JEV_HINTS / MEMORYMASTER_JEV_MODE
is ``live``, one request of seven nouls over the redacted prompt chooses the
hints (labels at or above their ``show`` threshold); ``shadow`` logs Jev's
labels and keeps the regex hints.  Every failure (timeout at the 900 ms hook
deadline, HTTP error, malformed answer, missing key) falls back to the regex
hints, and every decision is logged in the decisions ledger.  With nothing
configured the mode is ``off`` and the engine is not even imported, so the
output is byte-identical to the regex-only hook.
"""
import json
import os
import sys
import re
from datetime import datetime

PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
# Any of these set means the decisions config must be consulted; none set is
# the code default ``off`` (pinned by tests/test_classify_hook_jev.py).
_JEV_ENV = ("MEMORYMASTER_JEV_HINTS", "MEMORYMASTER_JEV_MODE", "MEMORYMASTER_DECISIONS_LOG_OFF")


def _log(event, **kw):
    """Minimal inline hook logger (no stdlib-external deps; stays ~5ms)."""
    try:
        log_dir = os.path.join(os.path.expanduser("~"), ".memorymaster", "hook_state")
        os.makedirs(log_dir, exist_ok=True)
        parts = [f"[{datetime.now().strftime('%H:%M:%S')}] hook=classify event={event}"]
        for k, v in kw.items():
            if v is None:
                continue
            parts.append(f"{k}={v}")
        with open(os.path.join(log_dir, "hook.log"), "a", encoding="utf-8") as fh:
            fh.write(" ".join(parts) + "\n")
    except Exception:
        pass


SIGNALS = [
    {
        "name": "DECISION",
        "message": (
            "DECISION detected — if this is a durable architectural/design "
            "decision, call mcp__memorymaster__ingest_claim with "
            "claim_type='decision', source_agent='claude-session'."
        ),
        "patterns": [
            # English
            "decided", "decision", "we chose", "we picked", "going with",
            "the call is", "we're going with", "agreed to", "agreed on",
            "let's go with", "final answer", "settled on",
            # Spanish (Argentina)
            "decidimos", "decidí", "elegimos", "vamos con", "nos quedamos con",
            "la decisión es", "la decision es", "quedó que", "quedo que",
            "acordamos", "zanjamos", "se definio", "se definió", "definimos",
            # Negative-decision (we are NOT going to do X) — durable "no-go"
            "no vamos a", "no lo vamos a", "olvidate", "olvidá",
            # Imperative command form signals a choice being made
            "más vale", "mas vale",
        ],
    },
    {
        "name": "BUG_ROOT_CAUSE",
        "message": (
            "BUG ROOT CAUSE detected — after fixing, call ingest_claim with "
            "claim_type='bug' and describe the root cause (not the fix) so "
            "future sessions avoid re-debugging it."
        ),
        "patterns": [
            # English — root-cause phrasing
            "root cause", "the bug is", "the bug was", "turned out to be",
            "was caused by", "is caused by", "fails because",
            "broken because", "regression", "was due to",
            # English — bug-report phrasing (user describing a failure)
            "dont work", "don't work", "doesn't work", "doesnt work",
            "not working", "is broken", "are broken",
            "help me debug", "debug this", "having an issue",
            "dropping error", "throwing error", "throws error", "throwing a",
            # HTTP error codes inside a bug description
            "429 error", "500 error", "502 error", "503 error", "504 error",
            "404 error", "400 error",
            # Spanish
            "la causa es", "la causa era", "el problema es", "el problema era",
            "se rompe cuando", "se rompia cuando", "se rompía cuando",
            "fallaba porque", "falla porque", "fue por", "era por",
            "el bug es", "el bug era", "resulta que",
            # Spanish — bug-report phrasing
            "no anda", "no funciona", "no andaba", "no funcionaba",
            "tira error", "tira eso", "sigue fallando", "se rompió",
            "se rompio", "está roto", "esta roto",
        ],
    },
    {
        "name": "GOTCHA",
        "message": (
            "GOTCHA detected — non-obvious trap worth remembering. Call "
            "ingest_claim with claim_type='gotcha', volatility='low'."
        ),
        "patterns": [
            # English
            "gotcha", "watch out", "be careful", "heads up", "caveat",
            "the trick is", "the catch is", "beware", "footgun",
            "silently fails", "silent failure", "surprising behavior",
            # Spanish
            "ojo con", "cuidado con", "cuidado que", "el truco es",
            "la trampa es", "atenti", "atención que", "atencion que",
            "no es obvio", "sorprendentemente", "silenciosamente",
            "falla en silencio",
        ],
    },
    {
        "name": "CONSTRAINT",
        "message": (
            "CONSTRAINT detected — hard limit or requirement. Call "
            "ingest_claim with claim_type='constraint' so future sessions "
            "respect it."
        ),
        "patterns": [
            # English
            "must not", "cannot", "can't", "never", "always", "required to",
            "requires", "mandatory", "forbidden", "not allowed", "only if",
            "only when", "must be", "has to be", "limited to",
            # English — numeric/technical limits (specs & rate limits).
            # Specific trailing colon/phrasing to avoid rhetorical matches.
            "rate limits:", "rate-limits:", "free tier limits",
            "hard limit", "soft limit", "token limits:",
            "requests per minute", "requests per day",
            "tokens per minute",
            # Spanish
            "no puede", "nunca", "siempre", "obligatorio",
            "obligatoria", "prohibido", "requiere", "tiene que", "debe ser",
            "debe estar", "no permitido", "solo si", "sólo si", "sólo cuando",
            "solo cuando", "limitado a",
            # Spanish — negated capabilities ("we don't have X", hard block).
            # Specific object heads (llave, acceso, permiso) avoid matching
            # idioms like "no tenemos nada que perder".
            "no tenemos llave", "no tenemos acceso",
            "no tenemos permiso", "no tenemos permisos",
            "no tenemos api", "sin llave", "no hay forma",
            # Explicit "we won't install X" is both decision & constraint
            "no lo instalamos", "no la instalamos",
        ],
    },
    {
        "name": "ARCHITECTURE",
        "message": (
            "ARCHITECTURE discussion detected — call "
            "mcp__memorymaster__query_memory BEFORE deciding, then "
            "ingest_claim with claim_type='architecture' after."
        ),
        "patterns": [
            # English
            "architecture", "system design", "refactor", "restructure",
            "rewrite", "data flow", "module boundary", "separation of concerns",
            "adr", "design doc", "trade-off", "tradeoff",
            # English — concrete architectural components
            "mcp server", "json-rpc", "stdio json", "knowledge base",
            "orchestrator", "vault structure", "exposes tools",
            "exposes 6 tools", "multi-session", "cross-session",
            # Spanish
            "arquitectura", "diseño del sistema", "diseno del sistema",
            "estructura", "refactor", "reescribir", "reestructurar",
            "separación", "separacion", "acoplamiento", "flujo de datos",
            "módulo", "modulo", "límites", "limites",
        ],
    },
    {
        "name": "ENVIRONMENT",
        "message": (
            "ENVIRONMENT/SETUP detected — install steps, env vars, config "
            "quirks. Call ingest_claim with claim_type='environment' so "
            "setup pain is documented."
        ),
        "patterns": [
            # English
            "env var", "environment variable", ".env", "export ",
            "install", "installing", "configure",
            "configuration", "path issue", "venv", "virtualenv",
            "dependency", "pip install", "npm install",
            # English — model/runtime switches (coding-agent specific)
            "using model", "using the model", "using opus", "using sonnet",
            "using haiku", "using gemini", "using gpt",
            "switched to", "switch model", "claude desktop",
            # Spanish
            "variable de entorno", "instalación", "instalacion",
            "configurar", "configuración", "configuracion", "entorno",
            "dependencia", "problema de path",
            # Spanish — model/runtime switches
            "usando el modelo", "estamos usando", "cambiamos al modelo",
            "cambié al modelo", "cambie al modelo",
            # MCP/agent runtime config changes
            "agregar mcp", "sacar el mcp", "agregar memorymaster",
        ],
    },
    {
        "name": "REFERENCE",
        "message": (
            "REFERENCE detected — docs/URL/paper/repo worth remembering. "
            "Call ingest_claim with claim_type='reference' and include the "
            "source in sources_json."
        ),
        "patterns": [
            # English
            "see the docs", "check the docs", "documentation says",
            "according to", "per the spec", "as documented",
            "github.com/", "https://", "http://",
            # Spanish
            "según la doc", "segun la doc", "según el", "segun el",
            "la doc dice", "documentación", "documentacion",
        ],
    },
]


def _any_word_match(pattern_words: list, text: str) -> bool:
    """Check if any phrase appears as a whole word/phrase.

    Conditional Latin-letter lookarounds: add (?<![a-zA-Z]) only if the
    phrase STARTS with a letter, and (?![a-zA-Z]) only if it ENDS with a
    letter. This prevents URL/symbol phrases like "https://" or
    "github.com/" from failing the right boundary when followed by a
    letter ("https://github..." — the 'g' after '/' used to block the
    match). Still safe for plain English keywords like "decided" /
    "cannot" — they have letter edges so both lookarounds apply.
    """
    for phrase in pattern_words:
        if not phrase:
            continue
        left = r'(?<![a-zA-Z])' if phrase[0].isalpha() else ''
        right = r'(?![a-zA-Z])' if phrase[-1].isalpha() else ''
        pat = left + re.escape(phrase) + right
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


# Meta-prompt wrappers injected by the Claude CLI that should NOT be
# classified (they're control messages, not user intent).
_META_STRIP_RE = re.compile(
    r'<local-command-caveat>.*?</local-command-caveat>',
    re.IGNORECASE | re.DOTALL,
)


def _strip_meta(text: str) -> str:
    """Remove Claude CLI meta-prompt wrappers before classification."""
    return _META_STRIP_RE.sub(' ', text)


def classify(prompt: str) -> list:
    cleaned = _strip_meta(prompt)
    signals = []
    for sig in SIGNALS:
        if _any_word_match(sig["patterns"], cleaned):
            signals.append((sig["name"], sig["message"]))
    return signals


# Jev labels (decisions.questions.HINT_LABELS) -> hint names.  GOTCHA has no Jev
# question, so only the regex fallback can show it.
_JEV_HINTS = {
    "decision": "DECISION", "constraint": "CONSTRAINT", "bug_root_cause": "BUG_ROOT_CAUSE",
    "environment": "ENVIRONMENT", "reference": "REFERENCE", "architecture": "ARCHITECTURE",
    "preference": "PREFERENCE",
}
_MESSAGES = {sig["name"]: sig["message"] for sig in SIGNALS}
_MESSAGES["PREFERENCE"] = (
    "PREFERENCE detected — how the user wants work done. Call ingest_claim "
    "with claim_type='preference' (scope='user' when it applies across projects)."
)
_ORDER = [sig["name"] for sig in SIGNALS] + ["PREFERENCE"]


def _ref(name: str) -> str:
    return "hint:" + name.lower()


_BY_REF = {_ref(name): name for name in _ORDER}


def jev_configured(environ) -> bool:
    return any((environ.get(name) or "").strip() for name in _JEV_ENV)


# The installed hook is killed at 5 s; the ledger's default 15 s busy wait on a
# decisions.db another process is writing would lose even the regex hints.
_LEDGER_BUSY_MS = 1000


def _hook_engine(jev):
    """An engine whose ledger waits at most ``_LEDGER_BUSY_MS`` for another writer."""
    from memorymaster.decisions.config import DecisionConfig

    config = DecisionConfig.from_env()
    ledger = None
    if config.mode_for("hints") != "off" or config.log_off:  # off never opens the ledger
        from memorymaster.decisions.ledger import DecisionLedger

        ledger = DecisionLedger(config.decisions_db, busy_timeout_ms=_LEDGER_BUSY_MS)
    return jev.DecisionEngine(config, ledger=ledger)


def _session_key(session_id):
    """The ledger's session key (F-11): the normalized, tenant-bound hash every hook
    shares (``recall.jev_surfaces.decision_session_key``), never the raw session id."""
    try:
        from memorymaster.recall.jev_surfaces import decision_session_key

        return decision_session_key(session_id)
    except Exception:
        return None


def select_hints(prompt: str, legacy: list, *, session_id=None, engine=None):
    """Hint names to show and a short Jev status for the log (``None`` when not consulted)."""
    if engine is None and not jev_configured(os.environ):
        return legacy, None
    try:
        if os.path.isdir(PROJECT_ROOT) and PROJECT_ROOT not in sys.path:
            sys.path.insert(0, PROJECT_ROOT)
        from memorymaster.decisions import engine as jev
        from memorymaster.decisions import questions
    except Exception:
        return legacy, "unavailable"

    def choose(answers):
        shown = set()
        for label in questions.HINT_LABELS:
            question_id = "hints." + label
            value = answers.noul(question_id)
            if value is None:
                raise ValueError("unanswered hint")
            if value >= answers.threshold(question_id, "show", 0.7):
                shown.add(_JEV_HINTS[label])
        return jev.JevChoice(action=[_ref(name) for name in _ORDER if name in shown])

    try:  # a hook and package out of step must not cost the regex hints
        state, bound = questions.build_hints(_strip_meta(prompt))
        decision = jev.decide(
            "hints", state=state, questions=bound,
            items=[jev.DecisionItem(_ref(name), "hint") for name in _ORDER],
            legacy_action=[_ref(name) for name in legacy], choose=choose,
            context=jev.DecisionContext(kind="hook", session_key=_session_key(session_id)),
            engine=engine or _hook_engine(jev),
        )
    except Exception:
        return legacy, "error"
    action = decision.action
    if not isinstance(action, list) or not all(ref in _BY_REF for ref in action):
        return legacy, "invalid"
    status = f"{decision.mode}:{decision.fallback_reason or 'ok'}"
    return [_BY_REF[ref] for ref in action], status


def render(names: list) -> str:
    hints = "\n".join(f"- [{name}] {_MESSAGES[name]}" for name in names)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "[MemoryMaster routing hints]\n"
                "The user's message contains signals. If the content is durable "
                "and non-obvious, consider calling ingest_claim AFTER doing the work:\n"
                + hints
                + "\n\nNever ingest credentials, IPs, tokens, or raw code."
            ),
        }
    }
    return json.dumps(output)


def run(input_data, *, engine=None):
    """The hook's stdout for one UserPromptSubmit payload, or ``None`` for no output."""
    prompt = input_data.get("prompt", "") if isinstance(input_data, dict) else ""
    if not isinstance(prompt, str) or len(prompt) < 5:
        _log("skip", reason="short-prompt", chars=len(prompt) if isinstance(prompt, str) else 0)
        return None

    try:
        legacy = [name for name, _ in classify(prompt)]
    except Exception as e:
        _log("error", message=str(e)[:200])
        return None

    names, jev = select_hints(prompt, legacy, session_id=input_data.get("session_id"), engine=engine)
    if not names:
        _log("no-match", chars=len(prompt), jev=jev)
        return None

    _log("matched", count=len(names), names=",".join(names), jev=jev)
    return render(names)


def main():
    try:
        input_data = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        sys.exit(0)

    output = run(input_data)
    if output is not None:
        sys.stdout.write(output)
        sys.stdout.flush()
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
