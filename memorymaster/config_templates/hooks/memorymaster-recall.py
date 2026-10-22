# MEMORYMASTER_MANAGED_RECALL_V1
# UserPromptSubmit recall hook. P1 WAL-discipline (spec §2.2): when the
# inherited environment carries MEMORYMASTER_WAL_DISCIPLINE=1, recall()
# below opens the DB strictly read-only (mode=ro + query_only — this
# per-prompt process can never take a write lock on the shared multi-GB
# file) and spools its access/feedback records to ~/.memorymaster/spool/
# for the steward drain. Flag unset/0 = the legacy read-write path.
import json
import os
import sys
import time

_T0 = time.perf_counter()  # T-1648: what the hook itself costs before it asks the server
PROJECT_ROOT = "__MEMORYMASTER_PROJECT_ROOT__"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")

sys.path.insert(0, PROJECT_ROOT)
os.environ["MEMORYMASTER_DEFAULT_DB"] = DB_PATH
os.chdir(PROJECT_ROOT)

from memorymaster.core.hook_log import log_hook  # noqa: E402 — import must follow sys.path bootstrap
from memorymaster.core.text_safety import sanitize_text  # noqa: E402
from memorymaster.recall.delivery import deliver, is_automated  # noqa: E402

try:  # 4.9.0+: recall_block and recall(hook_data=...) ship together
    from memorymaster.recall.delivery import recall_block  # noqa: E402
    JEV_OPT_IN = True
except ImportError:  # a package older than this template: legacy prefix, no Jev opt-in
    def recall_block(context):
        return "[MemoryMaster recall]\n" + context
    JEV_OPT_IN = False

try:  # T-1433: "ok"/"dale"/emoji get no recall, but a short real question ("why 500?") still does
    from memorymaster.recall.hook_policy import is_trivial_prompt  # noqa: E402
except ImportError:  # a package older than this template: the legacy word-count gate
    def is_trivial_prompt(prompt):
        return len(prompt.split()) < 3

try:  # T-1633: the one-shot residual a PreCompact hook stored; absent on a package older than this template
    from memorymaster.recall import residual as residual_state  # noqa: E402
except ImportError:
    residual_state = None

try:  # T-1632: MEMORYMASTER_RECALL_FORMAT=current|header|gated; unset or unknown = current (no change)
    from memorymaster.recall.recall_format import active_task_text, apply_format, format_mode  # noqa: E402
except ImportError:  # a package older than this template: the block exactly as recall rendered it
    def format_mode():
        return "current"

    def apply_format(ctx, prompt="", task="", mode=None):
        return ctx

    def active_task_text(cwd):
        return ""

try:  # T-1648: OS process age = interpreter start-up + imports; absent on an older package or off Windows
    from memorymaster.core.stall_probe import enabled as probe_enabled, process_age_ms  # noqa: E402
except ImportError:
    def probe_enabled():
        return False

    def process_age_ms():
        return None

_PROBE = probe_enabled()  # MEMORYMASTER_STALL_PROBE=0 turns off the OS lookup and the timing line
_IMPORT_MS = round((time.perf_counter() - _T0) * 1000, 1)
_BOOT_MS = process_age_ms() if _PROBE else None

try:
    data = json.loads(sys.stdin.read() or "{}")
    query = data.get("prompt", "")
    query = sanitize_text(query) if isinstance(query, str) else ""
    data["prompt"] = query
    session_id = str(data.get("session_id", ""))[:16]
    full_session = data.get("session_id")
    automated = is_automated(query)
    # The first prompt after a compaction is often "continue": the residual is read before the
    # trivial-prompt gate and is delivered by session id, never by similarity search.
    residual = ""
    if residual_state is not None and not automated and isinstance(full_session, str) and full_session:
        residual = residual_state.pending_block(full_session)
    ctx, via, timing = "", "", None
    if automated:
        log_hook("recall", "skip", session=session_id, reason="automated-event")
    elif is_trivial_prompt(query):
        log_hook("recall", "skip", session=session_id, reason="trivial-prompt", words=len(query.split()))
    else:
        _t = time.perf_counter()
        log_hook("recall", "start", session=session_id, query_len=len(query))
        log_ms = round((time.perf_counter() - _t) * 1000, 1)
        try:
            _t = time.perf_counter()
            from memorymaster.recall.remote import hook_recall  # T-0594: warm shared server first
            remote_import_ms = round((time.perf_counter() - _t) * 1000, 1)
            # hook_data opts into the S2 RECALL Jev surface (inert unless MEMORYMASTER_JEV_* is on).
            extra = {"hook_data": data} if JEV_OPT_IN else {}
            _t = time.perf_counter()
            ctx, via = hook_recall(query, extra.get("hook_data"), db_path=DB_PATH)
            recall_ms = round((time.perf_counter() - _t) * 1000, 1)
            _t = time.perf_counter()
            mode = format_mode()
            if ctx and mode != "current":
                claims_in = ctx.count("\n- [mm-")
                ctx = apply_format(ctx, query, active_task_text(data.get("cwd") or ""), mode)
                log_hook("recall", "format", session=session_id, mode=mode, claims_in=claims_in,
                         claims_out=ctx.count("\n- [mm-") if ctx else 0)
            format_ms = round((time.perf_counter() - _t) * 1000, 1)
            timing = {"log_ms": log_ms, "remote_import_ms": remote_import_ms, "recall_ms": recall_ms,
                      "format_ms": format_ms}
        except Exception as e:
            if not residual:
                raise
            log_hook("recall", "error", message=str(e)[:200])  # a recall failure must not drop the residual
    _t = time.perf_counter()
    parts = ([residual] if residual else []) + ([recall_block(ctx)] if ctx else [])
    if parts:
        def emit(output):
            sys.stdout.write(output)
            sys.stdout.flush()
        sent = deliver(data, "\n\n".join(parts), emit)
        if sent and residual:
            residual_state.consume(full_session)  # one block, once
        log_hook("recall", "done", session=session_id, hit=bool(ctx), residual=bool(residual), delivered=sent, via=via)
    # T-1648: logged for every recall outcome, including skipped_busy (the timeouts), which logged nothing before.
    if _PROBE and timing:
        log_hook("recall", "timing", session=session_id, via=via, import_ms=_IMPORT_MS, boot_ms=_BOOT_MS,
                 deliver_ms=round((time.perf_counter() - _t) * 1000, 1),
                 total_ms=round((time.perf_counter() - _T0) * 1000, 1), **timing)
except Exception as e:
    log_hook("recall", "error", message=str(e)[:200])
