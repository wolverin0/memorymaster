#!/bin/bash
# =============================================================================
# hermes-sync.sh — incremental delta sync between Hermes (Ubuntu VM) and the
# Windows MemoryMaster, over the SMB/CIFS mount.
# =============================================================================
#
# Replaces the whole-DB transfer (openclaw-sync.sh scp'd a 2.5 GB file twice
# per cycle) with a delta sync: each side exports ONLY claims changed since
# its last watermark into a tiny SQLite file, ships that, and merges it.
#
# Safety properties (this is why it's not openclaw-sync.sh):
#   - SQLite is NEVER opened on the SMB mount. Only `cp` touches mounted files.
#   - The whole 2.5 GB DB never crosses the network — only KB-sized deltas.
#   - merge-db is idempotent (dedups on idempotency_key) — re-running, or a
#     `>=` watermark re-export, can't create duplicates.
#   - flock prevents two cron cycles from racing.
#
# Cron (recommended cadence — twice a day, overnight does the heavy lifting):
#   0 3  * * * /opt/memorymaster/scripts/hermes-sync.sh >> ~/hermes-mm-sync.log 2>&1
#   0 15 * * * /opt/memorymaster/scripts/hermes-sync.sh >> ~/hermes-mm-sync.log 2>&1
#
# Environment variables (override as needed):
#   WIN_MOUNT_DIR  — SMB-mounted MemoryMaster dir (default: /mnt/pyapps/memorymaster)
#   LOCAL_DB       — Hermes-side MemoryMaster DB (default: /opt/memorymaster/hermes-memorymaster.db)
#   MM             — memorymaster CLI (default: memorymaster)
#   STATE_DIR      — where watermarks live (default: ~/.hermes-mm-sync)
# =============================================================================

set -euo pipefail

WIN_MOUNT_DIR="${WIN_MOUNT_DIR:-/mnt/pyapps/memorymaster}"
LOCAL_DB="${LOCAL_DB:-/opt/memorymaster/hermes-memorymaster.db}"
MM="${MM:-memorymaster}"
STATE_DIR="${STATE_DIR:-$HOME/.hermes-mm-sync}"

WIN_DB="$WIN_MOUNT_DIR/memorymaster.db"
DELTA_EXCHANGE_DIR="$WIN_MOUNT_DIR/delta-exchange"   # small delta files live here, on the share
TMP_DIR="/tmp/hermes-mm-sync"
LOCKFILE="/tmp/hermes-mm-sync.lock"

LOCAL_WATERMARK="$STATE_DIR/local.watermark"          # last updated_at we exported FROM local
WIN_WATERMARK="$STATE_DIR/windows.watermark"          # last updated_at we consumed FROM windows

mkdir -p "$STATE_DIR" "$TMP_DIR"

# --- single-flight ----------------------------------------------------------
exec 200>"$LOCKFILE"
flock -n 200 || { echo "$(date -Iseconds) SKIP: another sync in progress"; exit 0; }

echo "$(date -Iseconds) START delta sync"

# --- preflight: SMB mount reachable -----------------------------------------
if [ ! -r "$WIN_DB" ]; then
    echo "  WARN: Windows DB not readable at $WIN_DB — is the SMB mount up? Skipping."
    exit 0
fi

# =============================================================================
# DIRECTION 1: Hermes (local) -> Windows
# Export local claims newer than our last local watermark, drop the small
# delta on the share. The Windows side (or a Windows-side merge step) picks
# it up. We do NOT write the Windows DB ourselves over SMB.
# =============================================================================
LOCAL_SINCE=""
[ -f "$LOCAL_WATERMARK" ] && LOCAL_SINCE="$(cat "$LOCAL_WATERMARK")"

LOCAL_DELTA="$TMP_DIR/hermes-delta.db"
echo "  Exporting local delta since '${LOCAL_SINCE:-(full)}'..."
EXPORT_JSON="$($MM --json --db "$LOCAL_DB" export-delta --since "$LOCAL_SINCE" --output "$LOCAL_DELTA" 2>&1)" || {
    echo "  ERROR: local export-delta failed: $EXPORT_JSON"
    exit 1
}
echo "  $EXPORT_JSON"

# Copy the small delta onto the share for the Windows side to consume.
mkdir -p "$DELTA_EXCHANGE_DIR"
cp "$LOCAL_DELTA" "$DELTA_EXCHANGE_DIR/hermes-delta.db.new"
mv "$DELTA_EXCHANGE_DIR/hermes-delta.db.new" "$DELTA_EXCHANGE_DIR/hermes-delta.db"
echo "  Placed hermes delta on share: $DELTA_EXCHANGE_DIR/hermes-delta.db"

# Advance the local watermark to the newest updated_at we just exported.
NEW_LOCAL_WM="$(printf '%s' "$EXPORT_JSON" | grep -oE '"max_updated_at"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"([^"]*)"$/\1/' || true)"
if [ -n "$NEW_LOCAL_WM" ] && [ "$NEW_LOCAL_WM" != "null" ]; then
    echo "$NEW_LOCAL_WM" > "$LOCAL_WATERMARK"
    echo "  Advanced local watermark -> $NEW_LOCAL_WM"
fi

# =============================================================================
# DIRECTION 2: Windows -> Hermes (local)
# Copy the Windows DB to LOCAL disk (single cp — safe), export the delta
# of claims newer than our windows-watermark on the LOCAL copy, merge that
# small delta into the local Hermes DB.
#
# SQLite is opened only on local-disk paths: the cp'd copy and LOCAL_DB.
# =============================================================================
WIN_COPY="$TMP_DIR/windows.db"
echo "  Copying Windows DB to local disk for safe read..."
cp "$WIN_DB" "$WIN_COPY"

WIN_SINCE=""
[ -f "$WIN_WATERMARK" ] && WIN_SINCE="$(cat "$WIN_WATERMARK")"

WIN_DELTA="$TMP_DIR/windows-delta.db"
echo "  Exporting Windows delta since '${WIN_SINCE:-(full)}'..."
WIN_EXPORT_JSON="$($MM --json --db "$WIN_COPY" export-delta --since "$WIN_SINCE" --output "$WIN_DELTA" 2>&1)" || {
    echo "  ERROR: windows export-delta failed: $WIN_EXPORT_JSON"
    rm -f "$WIN_COPY"
    exit 1
}
echo "  $WIN_EXPORT_JSON"

echo "  Merging Windows delta into local Hermes DB..."
MERGE_JSON="$($MM --json --db "$LOCAL_DB" merge-db --source "$WIN_DELTA" 2>&1)" || {
    echo "  ERROR: merge-db failed: $MERGE_JSON"
    rm -f "$WIN_COPY" "$WIN_DELTA"
    exit 1
}
echo "  $MERGE_JSON"

NEW_WIN_WM="$(printf '%s' "$WIN_EXPORT_JSON" | grep -oE '"max_updated_at"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"([^"]*)"$/\1/' || true)"
if [ -n "$NEW_WIN_WM" ] && [ "$NEW_WIN_WM" != "null" ]; then
    echo "$NEW_WIN_WM" > "$WIN_WATERMARK"
    echo "  Advanced windows watermark -> $NEW_WIN_WM"
fi

# --- cleanup ----------------------------------------------------------------
rm -f "$WIN_COPY" "$WIN_COPY-shm" "$WIN_COPY-wal" "$WIN_DELTA" "$LOCAL_DELTA"

echo "$(date -Iseconds) DONE delta sync"
echo ""
echo "  NOTE: Direction 1 leaves hermes-delta.db on the share at:"
echo "    $DELTA_EXCHANGE_DIR/hermes-delta.db"
echo "  The Windows side must run, against its own DB:"
echo "    memorymaster --db <windows.db> merge-db --source <share>/delta-exchange/hermes-delta.db"
echo "  (the Windows-side merge is a separate scheduled step — see docs)"
