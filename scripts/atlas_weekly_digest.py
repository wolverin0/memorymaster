"""Atlas weekly digest — "tu semana" brief, straight from the brain.

Queries the recent life-claims, LLM-synthesizes a concise Spanish weekly digest
(grouped + prioritised by what's actionable/time-bound), pushes it to Telegram
via Jarvis (/api/telegram/test-send), and saves a local copy. Intended to run as
a weekly Windows Scheduled Task (Monday morning).

Env:
  MM_MODEL          Gemini model (default gemini-flash-lite-latest).
  DIGEST_DAYS       look-back window in days (default 8).
  DIGEST_NO_SEND    any value: save the digest, skip Telegram (same as --no-send).
  MEMORYMASTER_DB   claims database (default <repo>/memorymaster.db).
  GEMINI_API_KEY    else ~/.memorymaster/gemini-keys.env, else the file named by ATLAS_KEYFILE.
  ATLAS_VM          ssh target of the Jarvis host that relays to Telegram. Required to send;
                    no default because the address is operator-private.
  ATLAS_BASH        bash used to pipe the payload over ssh (default: bash on PATH, else Git for Windows).
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from a bare checkout too
from memorymaster.core.llm_provider import gemini_thinking_fields  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VM = os.environ.get("ATLAS_VM", "")
BRAIN = os.environ.get("MEMORYMASTER_DB") or str(Path(__file__).resolve().parents[1] / "memorymaster.db")
KEYFILE = os.environ.get("ATLAS_KEYFILE", "")
BASH = os.environ.get("ATLAS_BASH") or shutil.which("bash") or "C:/Program Files/Git/bin/bash.exe"
MODEL = os.environ.get("MM_MODEL", "gemini-flash-lite-latest")
DAYS = int(os.environ.get("DIGEST_DAYS", "8"))
OUTDIR = str(Path.home())


def _load_gemini_key():
    """env -> MM rotator file -> optional ATLAS_KEYFILE."""
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    if k:
        return k
    for p in (Path.home() / ".memorymaster" / "gemini-keys.env", KEYFILE):
        if not p:
            continue
        try:
            m = re.search(r"(?:AQ\.|AIza)[A-Za-z0-9_\-]{30,}",
                          open(p, encoding="utf-8", errors="ignore").read())
            if m:
                return m.group(0)
        except OSError:
            continue
    raise SystemExit("no Gemini key found (set GEMINI_API_KEY or ~/.memorymaster/gemini-keys.env)")


def recent_claims(db_path=None, days=None):
    c = sqlite3.connect(f"file:{Path(db_path or BRAIN).as_posix()}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT claim_type, subject, text, event_time FROM claims "
        "WHERE source_agent='atlas-llm-extractor' "
        "  AND created_at >= datetime('now', ?) "
        "ORDER BY (claim_type IN ('commitment','event','decision')) DESC, created_at DESC LIMIT 220",
        (f"-{days or DAYS} days",)).fetchall()
    c.close()
    return rows


def build_prompt(rows, hoy):
    lines = []
    for ct, subj, text, et in rows:
        d = f" [{et}]" if et else ""
        lines.append(f"- ({ct}) {subj}: {(text or '').strip()}{d}")
    corpus = "\n".join(lines)[:14000]
    return (
        f"Sos el asistente personal de Gonzalo. HOY es {hoy}. Abajo hay hechos TIPADOS de su vida "
        "(WhatsApp, mail, calendario). Escribi un brief de QUE TIENE POR DELANTE, breve y util, en "
        "espanol rioplatense, formato Telegram HTML (<b>negrita</b> para titulos, guiones para items; "
        "NADA de markdown ni #).\n\n"
        f"FILTRO CRITICO (lo mas importante de todo): incluí SOLO cosas FUTURAS o ABIERTAS. Descartá "
        f"TODO lo que ya paso — cualquier fecha ANTERIOR a HOY ({hoy}); por ejemplo si hoy es {hoy}, NO "
        "incluyas nada del 12/06, 13/06, 16/06 ni fechas previas. Descartá tambien lo que ya se "
        "resolvio/pago/cancelo. Si un tema aparece varias veces, usá SOLO el estado MAS RECIENTE. Ante "
        "la duda de si algo sigue vigente, OMITILO.\n\n"
        "Agrupa SOLO las secciones que tengan contenido futuro, en este orden:\n"
        "<b>⏰ Esta semana</b> (proximos ~7 dias, lo mas urgente primero, con fecha)\n"
        "<b>📅 Mas adelante</b> (deadlines futuros con fecha)\n"
        "<b>\U0001f4b8 Plata pendiente</b>\n<b>\U0001f4bc Trabajo</b>\n"
        "<b>\U0001f468‍\U0001f469‍\U0001f467 Familia</b>\n\n"
        "Reglas: MAXIMO 16 lineas en total; max 5 por seccion. Fusioná duplicados agresivamente. "
        "Priorizá fecha + monto concreto. Descartá micro-logistica (\"va en camino\", \"pasa en 20 min\", "
        "estados de juegos). Si NO hay nada futuro, escribi solo: 'Sin pendientes futuros detectados.' "
        "Empezá directo, sin preambulo.\n\n"
        f"HECHOS:\n{corpus}"
    )


def synthesize(rows):
    prompt = build_prompt(rows, time.strftime("%d/%m/%Y"))
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={_load_gemini_key()}"
    gen = {"temperature": 0.4, "maxOutputTokens": 1400}
    gen.update(gemini_thinking_fields(MODEL))
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}).encode()
    last = None
    for attempt in range(5):
        req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"}, method="POST")
        try:
            raw = urllib.request.urlopen(req, timeout=90).read().decode()
            return json.loads(raw)["candidates"][0]["content"]["parts"][0]["text"].strip()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (503, 429) and attempt < 4:
                time.sleep(6 * (attempt + 1))
                continue
            raise
    raise last


def telegram_message(text, today=None):
    header = f"<b>\U0001f9e0 Tu semana — {today or time.strftime('%d/%m/%Y')}</b>\n\n"
    return (header + text)[:4000]


def deliver_telegram(text):
    if not VM:
        raise SystemExit("ATLAS_VM is not set (ssh target of the Jarvis host); digest saved but not sent")
    msg = telegram_message(text)
    # robust: write payload to a temp file, pipe it via ssh stdin to curl -d @- (no quoting hell)
    tmp = os.path.join(OUTDIR, "_digest_payload.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"text": msg}, f, ensure_ascii=False)
    cmd = (f"cat '{tmp}' | ssh -o BatchMode=yes {VM} "
           f"'curl -sSf -m30 -X POST http://localhost:8788/api/telegram/test-send "
           f"-H \"content-type: application/json\" --data-binary @-'")
    r = subprocess.run([BASH, "-lc", cmd], capture_output=True, text=True, timeout=90)
    try:
        os.remove(tmp)
    except Exception:
        pass
    return r.returncode == 0, (r.stdout or r.stderr).strip()[:200]


def curation_tally() -> str:
    """Resumen de lo que la curacion automatica hizo esta semana.

    Es la mitad del contrato del dren (2026-08-26): la maquina resuelve lo
    no-operador, Y el operador recibe un resumen semanal de lo que hizo. Un
    dren silencioso seria una maquina reescribiendo memoria sin rendir cuentas.
    Se cuenta desde los eventos de auditoria, no desde un contador en memoria:
    la evidencia tiene que sobrevivir al proceso.
    """
    try:
        from datetime import datetime, timedelta, timezone

        desde = (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()
        con = sqlite3.connect(f"file:{Path(BRAIN).as_posix()}?mode=ro", uri=True)
        n = con.execute(
            "SELECT COUNT(*) FROM events WHERE details LIKE 'curation_drain%' AND created_at >= ?",
            (desde,),
        ).fetchone()[0]
        con.close()
        if not n:
            return ""
        return f"\n\n🧹 Curación automática: {n:,} resoluciones esta semana (auditadas; pinned y user quedaron para vos)."
    except Exception:
        return ""


def main():
    rows = recent_claims()
    if not rows:
        print("no recent claims for digest")
        return 0
    print(f"synthesizing digest from {len(rows)} recent claims on {MODEL}...", flush=True)
    digest = synthesize(rows) + curation_tally()
    path = os.path.join(OUTDIR, f"weekly-digest-{time.strftime('%Y-%m-%d')}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(digest)
    print(f"digest saved -> {path}", flush=True)
    if "--no-send" in sys.argv or os.environ.get("DIGEST_NO_SEND"):
        print("(no-send mode — telegram skipped)", flush=True)
        delivered = True
    else:
        delivered, detail = deliver_telegram(digest)
        print(f"telegram delivery {'ok' if delivered else 'FAILED'} -> {detail}", flush=True)
    print("\n--- DIGEST ---\n" + re.sub(r"<[^>]+>", "", digest))
    return 0 if delivered else 1


if __name__ == "__main__":
    raise SystemExit(main())
