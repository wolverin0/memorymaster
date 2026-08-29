"""Atlas weekly digest — "tu semana" brief, straight from the brain.

Queries the recent life-claims, LLM-synthesizes a concise Spanish weekly digest
(grouped + prioritised by what's actionable/time-bound), pushes it to Telegram
via Jarvis (/api/telegram/test-send), and saves a local copy. Intended to run as
a weekly Windows Scheduled Task (Monday morning).

Env: MM_MODEL (default gemini-2.5-flash-lite), DIGEST_DAYS (default 8), ATLAS_VM.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VM = os.environ.get("ATLAS_VM", "ggorbalan@192.168.100.186")
REPO = "G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster"
BRAIN = os.path.join(REPO, "memorymaster.db")
KEYFILE = os.environ.get("ATLAS_KEYFILE", "G:/_OneDrive/OneDrive/Desktop/new fiber.txt")
BASH = "C:/Program Files/Git/bin/bash.exe"
MODEL = os.environ.get("MM_MODEL", "gemini-2.5-flash-lite")
DAYS = int(os.environ.get("DIGEST_DAYS", "8"))
OUTDIR = "C:/Users/pauol"

def _load_gemini_key():
    """env -> MM rotator file -> legacy keyfile (cleaned after rotation)."""
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    if k:
        return k
    for p in (os.path.join("C:/Users/pauol", ".memorymaster", "gemini-keys.env"), KEYFILE):
        try:
            m = re.search(r"(?:AQ\.|AIza)[A-Za-z0-9_\-]{30,}",
                          open(p, encoding="utf-8", errors="ignore").read())
            if m:
                return m.group(0)
        except OSError:
            continue
    raise SystemExit("no Gemini key found (set GEMINI_API_KEY or ~/.memorymaster/gemini-keys.env)")


_key = _load_gemini_key()

TYPES = ("commitment", "event", "decision", "preference", "person", "fact", "project", "company", "product")


def recent_claims():
    c = sqlite3.connect(f"file:{BRAIN}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT claim_type, subject, text, event_time FROM claims "
        "WHERE source_agent='atlas-llm-extractor' "
        "  AND created_at >= datetime('now', ?) "
        "ORDER BY (claim_type IN ('commitment','event','decision')) DESC, created_at DESC LIMIT 220",
        (f"-{DAYS} days",)).fetchall()
    c.close()
    return rows


def synthesize(rows):
    lines = []
    for ct, subj, text, et in rows:
        d = f" [{et}]" if et else ""
        lines.append(f"- ({ct}) {subj}: {(text or '').strip()}{d}")
    corpus = "\n".join(lines)[:14000]
    hoy = time.strftime("%d/%m/%Y")
    prompt = (
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
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={_key}"
    gen = {"temperature": 0.4, "maxOutputTokens": 1400}
    if "gemini-2.5" in MODEL:
        gen["thinkingConfig"] = {"thinkingBudget": 0}
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
                time.sleep(6 * (attempt + 1)); continue
            raise
    raise last


def deliver_telegram(text):
    header = f"<b>\U0001f9e0 Tu semana — {time.strftime('%d/%m/%Y')}</b>\n\n"
    msg = (header + text)[:4000]
    # robust: write payload to a temp file, pipe it via ssh stdin to curl -d @- (no quoting hell)
    tmp = os.path.join(OUTDIR, "_digest_payload.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"text": msg}, f, ensure_ascii=False)
    cmd = (f"cat '{tmp}' | ssh -o BatchMode=yes {VM} "
           f"'curl -s -m30 -X POST http://localhost:8788/api/telegram/test-send "
           f"-H \"content-type: application/json\" --data-binary @-'")
    r = subprocess.run([BASH, "-lc", cmd], capture_output=True, text=True, timeout=90)
    try:
        os.remove(tmp)
    except Exception:
        pass
    return (r.stdout or r.stderr).strip()[:200]


def curation_tally() -> str:
    """Resumen de lo que la curacion automatica hizo esta semana.

    Es la mitad del contrato del dren (2026-08-26): la maquina resuelve lo
    no-operador, Y el operador recibe un resumen semanal de lo que hizo. Un
    dren silencioso seria una maquina reescribiendo memoria sin rendir cuentas.
    Se cuenta desde los eventos de auditoria, no desde un contador en memoria:
    la evidencia tiene que sobrevivir al proceso.
    """
    try:
        import sqlite3
        from datetime import datetime, timedelta, timezone

        db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memorymaster.db")
        desde = (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
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
        print("no recent claims for digest"); return 0
    print(f"synthesizing digest from {len(rows)} recent claims on {MODEL}...", flush=True)
    digest = synthesize(rows) + curation_tally()
    path = os.path.join(OUTDIR, f"weekly-digest-{time.strftime('%Y-%m-%d')}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(digest)
    print(f"digest saved -> {path}", flush=True)
    if "--no-send" in sys.argv or os.environ.get("DIGEST_NO_SEND"):
        print("(no-send mode — telegram skipped)", flush=True)
    else:
        print(f"telegram delivery -> {deliver_telegram(digest)}", flush=True)
    print("\n--- DIGEST ---\n" + re.sub(r"<[^>]+>", "", digest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
