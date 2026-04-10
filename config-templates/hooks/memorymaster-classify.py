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
"""
import json
import sys
import re


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
            # English
            "root cause", "the bug is", "the bug was", "turned out to be",
            "was caused by", "is caused by", "fails because",
            "broken because", "regression", "was due to",
            # Spanish
            "la causa es", "la causa era", "el problema es", "el problema era",
            "se rompe cuando", "se rompia cuando", "se rompía cuando",
            "fallaba porque", "falla porque", "fue por", "era por",
            "el bug es", "el bug era", "resulta que",
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
            # Spanish
            "no podemos", "no puede", "nunca", "siempre", "obligatorio",
            "obligatoria", "prohibido", "requiere", "tiene que", "debe ser",
            "debe estar", "no permitido", "solo si", "sólo si", "sólo cuando",
            "solo cuando", "limitado a",
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
            "install", "installing", "setup", "set up", "configure",
            "configuration", "path issue", "venv", "virtualenv",
            "dependency", "pip install", "npm install",
            # Spanish
            "variable de entorno", "instalar", "instalación", "instalacion",
            "configurar", "configuración", "configuracion", "entorno",
            "dependencia", "problema de path",
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

    Latin-letter lookarounds: (?<![a-zA-Z]) and (?![a-zA-Z]) ensure English
    keywords aren't part of a larger English word while allowing adjacency
    with Spanish accents or punctuation. Works with ñ, á, é, í, ó, ú.
    """
    for phrase in pattern_words:
        pat = r'(?<![a-zA-Z])' + re.escape(phrase) + r'(?![a-zA-Z])'
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def classify(prompt: str) -> list:
    signals = []
    for sig in SIGNALS:
        if _any_word_match(sig["patterns"], prompt):
            signals.append((sig["name"], sig["message"]))
    return signals


def main():
    try:
        input_data = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        sys.exit(0)

    prompt = input_data.get("prompt", "")
    if not isinstance(prompt, str) or len(prompt) < 5:
        sys.exit(0)

    try:
        signals = classify(prompt)
    except Exception:
        sys.exit(0)

    if not signals:
        sys.exit(0)

    hints = "\n".join(f"- [{name}] {msg}" for name, msg in signals)
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
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
