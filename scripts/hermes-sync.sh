#!/bin/bash
# =============================================================================
# hermes-sync.sh — incremental delta sync, Hermes (Ubuntu VM) side.
# =============================================================================
#
# Pairs with scripts/windows-hermes-sync.ps1 on the Windows side.
#
# Integration model (Option 1, decided 2026-05-18):
#   - Windows OWNS all writes to the Windows MemoryMaster DB.
#   - Hermes NEVER writes/replaces the Windows DB, and never copies the full
#     2.5 GB Windows DB. Hermes only:
#       (Direction 1) exports its own delta -> <share>/delta-exchange/hermes-delta.db
#       (Direction 2) consumes <share>/delta-exchange/windows-delta.db that the
#                     Windows scheduled task produced.
#
# Safety properties:
#   - SQLite is NEVER opened on the SMB mount — not even for reads. Both
#     delta files are `cp`'d to local /tmp first; SQLite only ever touches
#     local-disk paths.
#   - The whole 2.5 GB DB never crosses the network — only KB-sized deltas.
#   - merge-db is idempotent (dedups on idempotency_key + text-hash), so
#     re-consuming an unchanged windows-delta.db is a harmless no-op.
#   - flock prevents two cron cycles from racing.
#
# Cron (twice a day — memory claims are not latency-sensitive):
#   0 3  * * * /opt/memorymaster/scripts/hermes-sync.sh >> ~/hermes-mm-sync.log 2>&1
#   0 15 * * * /opt/memorymaster/scripts/hermes-sync.sh >> ~/hermes-mm-sync.log 2>&1
#
# Environment variables (override as needed):
#   WIN_MOUNT_DIR  — SMB-mounted MemoryMaster dir (default: /mnt/pyapps/memorymaster)
#   LOCAL_DB       — Hermes-side MemoryMaster DB (default: /opt/memorymaster/hermes-memorymaster.db)
#   MM             — memorymaster CLI (default: memorymaster)
#   STATE_DIR      — where the export watermark lives (default: ~/.hermes-mm-sync)
# =============================================================================

set -euo pipefail

WIN_MOUNT_DIR="${WIN_MOUNT_DIR:-/mnt/pyapps/memorymaster}"
LOCAL_DB="${LOCAL_DB:-/opt/memorymaster/hermes-memorymaster.db}"
MM="${MM:-memorymaster}"
STATE_DIR="${STATE_DIR:-$HOME/.hermes-mm-sync}"

DELTA_EXCHANGE_DIR="$WIN_MOUNT_DIR/delta-exchange"
TMP_DIR="/tmp/hermes-mm-sync"
LOCKFILE="/tmp/hermes-mm-sync.lock"

LOCAL_WATERMARK="$STATE_DIR/local.watermark"   # newest updated_at Hermes has exported

mkdir -p "$STATE_DIR" "$TMP_DIR"

# --- single-flight ----------------------------------------------------------
exec 200>"$LOCKFILE"
flock -n 200 || { echo "$(date -Iseconds) SKIP: another sync in progress"; exit 0; }

echo "$(date -Iseconds) START delta sync"

# --- preflight: SMB exchange dir reachable ----------------------------------
if [ ! -d "$WIN_MOUNT_DIR" ]; then
    echo "  WARN: SMB mount dir $WIN_MOUNT_DIR not present — is the mount up? Skipping."
    exit 0
fi
mkdir -p "$DELTA_EXCHANGE_DIR"

# =============================================================================
# DIRECTION 1: Hermes -> Windows
# Export Hermes's own delta (claims newer than our last export watermark)
# and place the small file on the share. The Windows scheduled task merges it.
# =============================================================================
LOCAL_SINCE=""
[ -f "$LOCAL_WATERMARK" ] && LOCAL_SINCE="$(cat "$LOCAL_WATERMARK")"

LOCAL_DELTA="$TMP_DIR/hermes-delta.db"
echo "  Exporting Hermes delta since '${LOCAL_SINCE:-(full)}'..."
EXPORT_JSON="$($MM --json --db "$LOCAL_DB" export-delta --since "$LOCAL_SINCE" --output "$LOCAL_DELTA" 2>&1)" || {
    echo "  ERROR: hermes export-delta failed: $EXPORT_JSON"
    exit 1
}
echo "  $EXPORT_JSON"

# Place on the share via atomic .new + mv (a plain file copy — no SQLite-over-SMB).
cp "$LOCAL_DELTA" "$DELTA_EXCHANGE_DIR/hermes-delta.db.new"
mv "$DELTA_EXCHANGE_DIR/hermes-delta.db.new" "$DELTA_EXCHANGE_DIR/hermes-delta.db"
echo "  Placed hermes delta on share: $DELTA_EXCHANGE_DIR/hermes-delta.db"

# Advance the export watermark to the newest updated_at we just exported.
NEW_LOCAL_WM="$(printf '%s' "$EXPORT_JSON" | grep -oE '"max_updated_at"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"([^"]*)"$/\1/' || true)"
if [ -n "$NEW_LOCAL_WM" ] && [ "$NEW_LOCAL_WM" != "null" ]; then
    echo "$NEW_LOCAL_WM" > "$LOCAL_WATERMARK"
    echo "  Advanced hermes export watermark -> $NEW_LOCAL_WM"
fi

# =============================================================================
# DIRECTION 2: Windows -> Hermes
# Consume the small windows-delta.db the Windows scheduled task produced.
# Copy it to local /tmp first so SQLite never opens a file on the SMB mount,
# then merge it into the local Hermes DB. No full-DB copy, ever.
# =============================================================================
WIN_DELTA_REMOTE="$DELTA_EXCHANGE_DIR/windows-delta.db"
WIN_DELTA_LOCAL="$TMP_DIR/windows-delta.db"

if [ -f "$WIN_DELTA_REMOTE" ]; then
    echo "  Copying windows-delta.db off the share to local disk..."
    cp "$WIN_DELTA_REMOTE" "$WIN_DELTA_LOCAL"
    echo "  Merging windows delta into local Hermes DB..."
    MERGE_JSON="$($MM --json --db "$LOCAL_DB" merge-db --source "$WIN_DELTA_LOCAL" 2>&1)" || {
        echo "  ERROR: merge-db failed: $MERGE_JSON"
        rm -f "$WIN_DELTA_LOCAL"
        exit 1
    }
    echo "  $MERGE_JSON"
else
    echo "  No windows-delta.db on the share yet — Windows scheduled task"
    echo "  has not exported. Skipping inbound merge (will retry next cycle)."
fi

# --- cleanup ----------------------------------------------------------------
rm -f "$LOCAL_DELTA" "$WIN_DELTA_LOCAL"

echo "$(date -Iseconds) DONE delta sync"
