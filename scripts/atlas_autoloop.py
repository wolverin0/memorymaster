"""Atlas auto-loop — keep the brain fed automatically.

One pass: pull the VM's atlas-inbox.db -> find evidence newer than the last
watermark -> batched LLM extraction (fits the free-tier request cap) -> merge
the new typed claims into the brain. The existing steward cron then governs the
new `candidate`s. Designed to run unattended as a daily Windows Scheduled Task.

Watermark in a JSON state file means each run only processes NEW evidence, so
the daily request volume is tiny and quota-safe. On the very first run it just
records the current high-water mark (no re-processing of already-backfilled
history) unless --reprocess-all is passed.

Note: this is the EXTRACTION half of the loop. It only sees new evidence if the
VM's Atlas harvesters (Gmail/Outlook/WhatsApp imports) are actually running.

Env: MM_MODEL (default gemini-2.5-flash-lite), VM (ssh target), MEMORYMASTER_LLM key file.
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
import concurrent.futures as cf

VM = os.environ.get("ATLAS_VM", "ggorbalan@192.168.100.186")
REPO = "G:/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster"
BRAIN = os.path.join(REPO, "memorymaster.db")
KEYFILE = os.environ.get("ATLAS_KEYFILE", "G:/_OneDrive/OneDrive/Desktop/new fiber.txt")
STATE = "C:/Users/pauol/.atlas_autoloop_state.json"
WORK = "C:/Users/pauol/atlas-autoloop.db"
LOG = "C:/Users/pauol/atlas-autoloop.log"
BASH = "C:/Program Files/Git/bin/bash.exe"
# 3.1-flash-lite-preview: ~500 req/day free per key; 2.5-flash-lite was 429 on
# ALL rotator keys (verified 2026-07-02) and silently zeroed a whole night's
# extraction (2026-07-03 run: 7 batches, 0 claims).
MODEL = os.environ.get("MM_MODEL", "gemini-3.1-flash-lite-preview")
BATCH = int(os.environ.get("BATCH", "20"))
WORKERS = int(os.environ.get("WORKERS", "4"))
MAX_PER_RUN = int(os.environ.get("MAX_EVIDENCE_PER_RUN", "8000"))  # safety cap


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


IMPORT_SOURCES = ["gmail", "outlook-mail", "google-calendar", "outlook-calendar", "google-drive", "onedrive"]


def trigger_vm_imports():
    """POST each source's /import on the VM so fresh evidence is harvested before we pull.
    Best-effort: per-source timeout, failures are non-fatal (logged)."""
    eps = " ".join(IMPORT_SOURCES)
    cmd = (
        f"ssh -o BatchMode=yes {VM} 'for ep in {eps}; do "
        f"curl -s -m90 -X POST http://localhost:8788/api/$ep/import "
        f"-H \"content-type: application/json\" -d \"{{}}\" -o /dev/null "
        f"-w \"$ep:%{{http_code}} \"; done; echo'"
    )
    try:
        r = subprocess.run([BASH, "-lc", cmd], capture_output=True, text=True, timeout=720)
        log(f"VM imports triggered -> {r.stdout.strip()[:200]}")
    except Exception as e:
        log(f"VM import trigger failed (non-fatal): {str(e)[:120]}")


def pull_vm_db():
    """docker cp the live atlas-inbox.db off the VM to WORK (via git-bash scp)."""
    cmd = (
        f"ssh -o BatchMode=yes {VM} "
        f"'docker cp atlas-inbox:/data/atlas-inbox.db /tmp/al_autoloop.db && chmod 644 /tmp/al_autoloop.db' && "
        f"MSYS_NO_PATHCONV=1 scp -o BatchMode=yes {VM}:/tmp/al_autoloop.db '{WORK}' && "
        f"ssh -o BatchMode=yes {VM} 'rm -f /tmp/al_autoloop.db'"
    )
    subprocess.run([BASH, "-lc", cmd], check=True, timeout=180)


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"watermark": None, "last_run": None, "total_ingested": 0}


def save_state(state):
    tmp = STATE + ".tmp"
    json.dump(state, open(tmp, "w", encoding="utf-8"), indent=2)
    os.replace(tmp, STATE)


# ---- batched extraction (self-contained; reuses the package validator/ingest) ----
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
os.environ["MEMORYMASTER_LLM_API_KEYS"] = _key
os.environ["MEMORYMASTER_LLM_PROVIDER"] = "google"
sys.path.insert(0, REPO)
import memorymaster.bridges.atlas_llm_extractor as ax  # noqa: E402
from memorymaster.core.service import MemoryService  # noqa: E402

_BATCH_INSTR = (
    "\n\nYou will now receive MULTIPLE numbered items. Apply ALL the rules above to "
    "EACH item independently. Return ONE flat JSON ARRAY of the claim objects from "
    "ALL items; each object MUST include an extra integer field \"item\" equal to the "
    "item number it came from. Return [] if none qualify. No prose, no code fence.\n\nITEMS:\n"
)


def _prompt(batch):
    parts = [ax._PROMPT_BODY, _BATCH_INSTR]
    for i, (ev, si) in enumerate(batch):
        snd = (getattr(si, "sender_name", None) or "unknown") if si else "unknown"
        parts.append(f"[{i}] provider={ev.provider or 'unknown'} sender={snd}\n{(ev.text or '').strip()[:700]}\n")
    return "".join(parts)


def _call(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={_key}"
    gen = {"temperature": 0.1, "maxOutputTokens": 8192, "responseMimeType": "application/json"}
    if "gemini-2.5" in MODEL:
        gen["thinkingConfig"] = {"thinkingBudget": 0}
    elif "gemini-3" in MODEL:
        gen["thinkingConfig"] = {"thinkingLevel": "minimal"}
    req = urllib.request.Request(
        url, data=json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}).encode(),
        headers={"content-type": "application/json"}, method="POST")
    raw = urllib.request.urlopen(req, timeout=90).read().decode()
    return json.loads(raw)["candidates"][0]["content"]["parts"][0]["text"]


def _parse(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`"); raw = raw[raw.find("["):]
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else None
    except Exception:
        m = re.search(r"\[.*\]", raw, re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except Exception:
            return None


def extract_and_merge(pairs):
    """Batched-extract `pairs` straight into the brain (ingest dedups by idempotency)."""
    svc = MemoryService(BRAIN)
    batches = [pairs[i:i + BATCH] for i in range(0, len(pairs), BATCH)]
    log(f"extracting {len(pairs)} new evidence in {len(batches)} batched requests on {MODEL}")

    def work(batch):
        try:
            return batch, _parse(_call(_prompt(batch)))
        except Exception as e:
            return batch, None

    results = []
    with cf.ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(work, batches))
    ing = 0
    failed = 0
    for batch, rows in results:
        if not isinstance(rows, list):
            # LLM call errored or returned garbage for this whole batch — this
            # is EXTRACTION FAILURE, not "no claims in this evidence". Count it
            # so main() can refuse to advance the watermark past lost items
            # (the 2026-07-03 run advanced past 131 items extracted as 0).
            failed += 1
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                idx = int(row.get("item"))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(batch):
                typed = ax._validate_row(row)
                if typed is not None:
                    ev, si = batch[idx]
                    try:
                        ax._ingest_typed_claim(svc, ev, si, typed, scope="user"); ing += 1
                    except Exception:
                        pass
    return ing, failed, len(batches)


def main():
    reprocess = "--reprocess-all" in sys.argv
    log("=== autoloop start ===")
    trigger_vm_imports()
    try:
        pull_vm_db()
    except Exception as e:
        log(f"VM pull FAILED: {str(e)[:120]} — aborting run"); return 1

    state = load_state()
    wm = state.get("watermark")
    con = sqlite3.connect(f"file:{WORK}?mode=ro", uri=True)
    if reprocess or wm is None:
        new_rows = con.execute(
            "SELECT id, source_item_id FROM evidence_items WHERE text IS NOT NULL AND trim(text)!='' ORDER BY created_at"
        ).fetchall() if reprocess else []
        newest = con.execute("SELECT MAX(created_at) FROM evidence_items").fetchone()[0]
        if wm is None and not reprocess:
            # first run: just set the watermark, don't reprocess history
            con.close()
            state.update({"watermark": newest, "last_run": time.strftime("%Y-%m-%dT%H:%M:%S")})
            save_state(state)
            log(f"first run — watermark set to {newest}; no history reprocessed (use --reprocess-all to backfill)")
            log("=== autoloop done (no-op) ==="); return 0
    else:
        new_rows = con.execute(
            "SELECT id, source_item_id FROM evidence_items WHERE created_at > ? AND text IS NOT NULL AND trim(text)!='' ORDER BY created_at",
            (wm,)).fetchall()
    newest = con.execute("SELECT MAX(created_at) FROM evidence_items").fetchone()[0]
    con.close()

    if not new_rows:
        state.update({"last_run": time.strftime("%Y-%m-%dT%H:%M:%S")})
        save_state(state)
        log("no new evidence since last run"); log("=== autoloop done (no-op) ==="); return 0

    new_rows = new_rows[:MAX_PER_RUN]
    # hydrate EvidenceItem + SourceItem via the service against the work DB
    svc_work = MemoryService(WORK)
    pairs = []
    for eid, _sid in new_rows:
        ev = next((e for e in svc_work.list_evidence_items(limit=100000) if e.id == eid), None)
        if ev is None:
            continue
        si = svc_work.get_source_item_by_id(ev.source_item_id)
        pairs.append((ev, si))
    ing, failed, total_batches = extract_and_merge(pairs)
    if failed:
        # Do NOT advance the watermark: the failed batches' evidence would be
        # skipped forever (silent data loss). Successful batches' claims are
        # already ingested — re-extracting them next run is a cheap idempotent
        # no-op (ingest dedups). Exit 1 so the scheduled task's Last Result
        # flags the failure instead of reporting success.
        state.update({"last_run": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "total_ingested": state.get("total_ingested", 0) + ing})
        save_state(state)
        log(f"ERROR: {failed}/{total_batches} extraction batches FAILED (model={MODEL}); "
            f"ingested {ing} from successful batches; watermark NOT advanced (stays {wm})")
        log("=== autoloop done (extraction failures) ==="); return 1
    state.update({"watermark": newest, "last_run": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "total_ingested": state.get("total_ingested", 0) + ing})
    save_state(state)
    log(f"ingested {ing} new claims into the brain; watermark -> {newest}")
    log("=== autoloop done ==="); return 0


if __name__ == "__main__":
    raise SystemExit(main())
