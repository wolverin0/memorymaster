#!/bin/bash
# Bidirectional MemoryMaster sync between OpenClaw (Linux) and Windows PC.
#
# Replaces the old OVERWRITE sync with a proper MERGE:
#   1. Download Windows DB to temp file
#   2. Merge Windows claims → Local DB (new claims from Windows)
#   3. Merge Local claims → Windows temp DB (new claims from OpenClaw)
#   4. Upload merged DB back to Windows
#
# Run via cron every 15 min:
#   */15 * * * * /opt/memorymaster/scripts/openclaw-sync.sh >> /var/log/memorymaster-sync.log 2>&1
#
# Environment variables:
#   WINDOWS_HOST    — SSH host for Windows PC (default: windows-pc)
#   WINDOWS_DB_PATH — Remote path to memorymaster.db
#   LOCAL_DB        — Local memorymaster.db path
#   MEMORYMASTER_BIN — Path to memorymaster CLI (default: memorymaster)

set -euo pipefail

WINDOWS_HOST="${WINDOWS_HOST:-windows-pc}"
WINDOWS_DB_PATH="${WINDOWS_DB_PATH:-/g/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster/memorymaster.db}"
LOCAL_DB="${LOCAL_DB:-/opt/memorymaster/memorymaster.db}"
MM="${MEMORYMASTER_BIN:-memorymaster}"
TMP_DIR="/tmp/memorymaster-sync"
LOCKFILE="/tmp/memorymaster-sync.lock"

# Prevent concurrent syncs
exec 200>"$LOCKFILE"
flock -n 200 || { echo "$(date -Iseconds) SKIP: another sync in progress"; exit 0; }

mkdir -p "$TMP_DIR"
WINDOWS_COPY="$TMP_DIR/windows.db"

echo "$(date -Iseconds) START bidirectional sync"

# 1. Download Windows DB
echo "  Downloading from $WINDOWS_HOST..."
if ! scp -q "$WINDOWS_HOST:$WINDOWS_DB_PATH" "$WINDOWS_COPY" 2>/dev/null; then
    echo "  WARN: Windows host unreachable, skipping sync"
    exit 0
fi

# 2. Merge Windows → Local (get new claims from Windows)
echo "  Merging Windows → Local..."
MERGE_IN=$($MM --db "$LOCAL_DB" merge-db --source "$WINDOWS_COPY" 2>&1) || true
echo "  $MERGE_IN"

# 3. Merge Local → Windows copy (push local claims to Windows)
echo "  Merging Local → Windows copy..."
MERGE_OUT=$($MM --db "$WINDOWS_COPY" merge-db --source "$LOCAL_DB" 2>&1) || true
echo "  $MERGE_OUT"

# 4. Upload merged DB back to Windows
echo "  Uploading merged DB to $WINDOWS_HOST..."
if scp -q "$WINDOWS_COPY" "$WINDOWS_HOST:$WINDOWS_DB_PATH" 2>/dev/null; then
    echo "  Upload OK"
else
    echo "  WARN: Upload failed, local changes preserved but not pushed"
fi

# Cleanup
rm -f "$WINDOWS_COPY"

echo "$(date -Iseconds) DONE"
